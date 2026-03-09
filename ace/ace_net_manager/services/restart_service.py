from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ace_net_manager.settings import ManagerSettings
from ace_net_manager.utils import naming
from ace_net_manager.utils.errors import OperationError


class RestartService:
    def __init__(
        self,
        *,
        settings: ManagerSettings,
        runtime_service,
        docker_service,
        health_service,
        deploy_service,
    ) -> None:
        self._settings = settings
        self._runtime = runtime_service
        self._docker = docker_service
        self._health = health_service
        self._deploy = deploy_service

    def _volume_labels(self, user_id: str) -> dict[str, str]:
        return {
            "ace.managed": "true",
            "ace.user_id": user_id,
            "ace.created_by": "ace-control-plane",
        }

    async def restart_user(
        self,
        db: Session,
        *,
        user_id: str,
        mode: str,
        operation_id: str,
        requested_by: str = "admin",
    ) -> dict[str, Any]:
        runtime = self._runtime.get_runtime(db, user_id)
        if runtime is None:
            raise OperationError("runtime not found", code="runtime_missing")

        requested_mode = (mode or "safe").strip().lower()
        if requested_mode not in {"safe", "force"}:
            raise OperationError("invalid restart mode", code="invalid_restart_mode")
        if requested_mode == "force" and requested_by != "admin":
            raise OperationError("force restart is admin-only", code="forbidden_force_restart")

        if requested_mode == "safe":
            image_tag = runtime.get("active_image_tag")
            if not image_tag:
                raise OperationError("runtime has no active image tag", code="invalid_runtime_state")
            active_generation = int(runtime.get("current_generation") or 0)
            active_row = self._runtime.get_generation(db, user_id=user_id, generation=active_generation) if active_generation else None
            return await self._deploy.deploy_user(
                db,
                user_id=user_id,
                image_tag=str(image_tag),
                env_overrides=naming.env_overrides_from_generation(active_row),
                keep_previous_warm_seconds=0,
                operation_id=operation_id,
            )

        # force mode
        current_generation = int(runtime.get("current_generation") or 0)
        active_container = runtime.get("active_container_name")
        image_tag = runtime.get("active_image_tag")
        if not active_container or not image_tag:
            raise OperationError("runtime has no active container", code="invalid_runtime_state")

        new_generation = current_generation + 1
        new_container_name = naming.container_name(user_id, new_generation)
        labels = naming.container_labels(user_id, new_generation, "active", str(image_tag))
        active_row = self._runtime.get_generation(db, user_id=user_id, generation=current_generation) if current_generation else None
        active_env_overrides = naming.env_overrides_from_generation(active_row)
        env = naming.runtime_env(
            user_id=user_id,
            generation=new_generation,
            role="active",
            env_overrides=active_env_overrides,
            internal_port=self._settings.ace_agent_internal_port,
            data_mount=self._settings.ace_agent_data_mount,
            skills_mount=self._settings.ace_runtime_skills_mount,
        )
        generation_metadata = naming.generation_metadata(
            user_id=user_id,
            generation=new_generation,
            role="active",
            image_tag=str(image_tag),
            env_overrides=active_env_overrides,
        )
        volume_name = runtime.get("volume_name") or naming.volume_name(user_id)
        network_name = runtime.get("network_name") or self._settings.ace_docker_network
        self._runtime.update_runtime(
            db,
            user_id,
            status="restarting",
            desired_image_tag=str(image_tag),
        )
        self._runtime.create_generation(
            db,
            user_id=user_id,
            generation=new_generation,
            container_name=new_container_name,
            image_tag=str(image_tag),
            role="active",
            lifecycle_state="creating",
            docker_labels_json=generation_metadata,
        )
        db.commit()

        try:
            self._docker.ensure_volume(volume_name, labels=self._volume_labels(user_id))
            self._docker.assert_container_matches(
                str(active_container),
                image_tag=str(image_tag),
                network_name=str(network_name),
                volume_name=str(volume_name),
                labels=naming.container_labels(user_id, current_generation, "active", str(image_tag)),
            )
            self._docker.stop_and_remove_container(str(active_container), timeout=self._settings.ace_drain_timeout_seconds)
            container_id = self._docker.create_container(
                name=new_container_name,
                image_tag=str(image_tag),
                network_name=network_name,
                volume_name=volume_name,
                labels=labels,
                environment=env,
                internal_port=self._settings.ace_agent_internal_port,
                global_skills_dir=self._settings.ace_global_skills_dir,
                runtime_skills_mount=self._settings.ace_runtime_skills_mount,
            )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=new_generation,
                lifecycle_state="starting",
                container_id=container_id,
                docker_labels_json=generation_metadata,
            )
            db.commit()

            self._docker.start_container(new_container_name)
            ready = await self._health.wait_until_ready(new_container_name)
            if not ready:
                raise OperationError("forced restart container failed readiness", code="readiness_timeout")

            old_active = self._runtime.atomic_cutover(
                db,
                user_id=user_id,
                new_generation=new_generation,
                new_container_name=new_container_name,
                new_container_id=container_id,
                new_image_tag=str(image_tag),
            )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=new_generation,
                role="active",
                lifecycle_state="promoted",
                promoted_at="now()",
                health_status="healthy",
            )
            old_generation = old_active.get("generation")
            if old_generation:
                self._runtime.update_generation(
                    db,
                    user_id=user_id,
                    generation=int(old_generation),
                    role="retired",
                    lifecycle_state="removed",
                    retired_at="now()",
                )
            self._runtime.update_runtime(
                db,
                user_id,
                status="active",
                desired_image_tag=None,
                last_health_status="healthy",
                last_health_checked_at="now()",
            )
            db.commit()
            return {
                "user_id": user_id,
                "old_generation": current_generation,
                "new_generation": new_generation,
                "mode": "force",
            }
        except Exception as exc:
            self._docker.cleanup_failed_container(
                new_container_name,
                timeout=self._settings.ace_drain_timeout_seconds,
                preserve=self._settings.ace_preserve_failed_containers,
                preserved_name=naming.failed_container_name(user_id, new_generation, operation_id),
            )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=new_generation,
                role="failed",
                lifecycle_state="failed",
                health_status="failed",
                health_summary=str(exc),
            )
            self._runtime.update_runtime(db, user_id, status="failed", last_health_status="failed")
            db.commit()
            if isinstance(exc, OperationError):
                raise
            raise OperationError(f"force restart failed: {exc}", code="restart_failed") from exc
