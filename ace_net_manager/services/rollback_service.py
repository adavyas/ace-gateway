from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ace_net_manager.settings import ManagerSettings
from ace_net_manager.utils import naming
from ace_net_manager.utils.errors import OperationError


class RollbackService:
    def __init__(
        self,
        *,
        settings: ManagerSettings,
        runtime_service,
        docker_service,
        health_service,
    ) -> None:
        self._settings = settings
        self._runtime = runtime_service
        self._docker = docker_service
        self._health = health_service

    def _volume_labels(self, user_id: str) -> dict[str, str]:
        return {
            "ace.managed": "true",
            "ace.user_id": user_id,
            "ace.created_by": "ace-control-plane",
        }

    async def rollback_user(
        self,
        db: Session,
        *,
        user_id: str,
        reason: str | None,
        operation_id: str,
        from_failed_deploy: bool = False,
    ) -> dict[str, Any]:
        runtime = self._runtime.get_runtime(db, user_id)
        if runtime is None:
            raise OperationError("runtime not found", code="runtime_missing")

        current_generation = runtime.get("current_generation")
        previous_image_tag = runtime.get("previous_image_tag")
        self._runtime.update_runtime(db, user_id, status="rollback_pending")
        db.commit()

        previous_generation = runtime.get("previous_generation")
        previous_row = None
        if not previous_image_tag and previous_generation:
            previous_row = self._runtime.get_generation(
                db,
                user_id=user_id,
                generation=int(previous_generation),
            )
            if previous_row:
                previous_image_tag = previous_row.get("image_tag")
        elif previous_generation:
            previous_row = self._runtime.get_generation(
                db,
                user_id=user_id,
                generation=int(previous_generation),
            )
        if not previous_image_tag:
            raise OperationError("no previous image to rollback to", code="rollback_target_missing")

        network_name = runtime.get("network_name") or self._settings.ace_docker_network
        volume_name = runtime.get("volume_name") or naming.volume_name(user_id)
        current_container_name = runtime.get("active_container_name") or naming.container_name(
            user_id, int(current_generation or 0)
        )
        current_image_tag = runtime.get("active_image_tag")
        new_generation = int(runtime.get("current_generation") or 0) + 1
        target_generation = new_generation
        target_image_tag = str(previous_image_tag)
        target_container_name = naming.container_name(user_id, new_generation)
        labels = naming.container_labels(user_id, new_generation, "active", target_image_tag)
        target_env_overrides = naming.env_overrides_from_generation(previous_row)
        env = naming.runtime_env(
            user_id=user_id,
            generation=new_generation,
            role="active",
            env_overrides=target_env_overrides,
            internal_port=self._settings.ace_agent_internal_port,
            data_mount=self._settings.ace_agent_data_mount,
            skills_mount=self._settings.ace_runtime_skills_mount,
        )
        generation_metadata = naming.generation_metadata(
            user_id=user_id,
            generation=new_generation,
            role="active",
            image_tag=target_image_tag,
            env_overrides=target_env_overrides,
        )
        self._runtime.create_generation(
            db,
            user_id=user_id,
            generation=new_generation,
            container_name=target_container_name,
            image_tag=target_image_tag,
            role="active",
            lifecycle_state="creating",
            docker_labels_json=generation_metadata,
            rollback_to_image_tag=target_image_tag,
        )
        db.commit()

        try:
            self._docker.ensure_volume(volume_name, labels=self._volume_labels(user_id))
            if current_container_name and current_image_tag:
                self._docker.assert_container_matches(
                    str(current_container_name),
                    image_tag=str(current_image_tag),
                    network_name=str(network_name),
                    volume_name=str(volume_name),
                    labels=naming.container_labels(user_id, int(current_generation or 0), "active", str(current_image_tag)),
                )
            self._docker.pull_image(target_image_tag)
            if current_container_name:
                self._docker.stop_and_remove_container(
                    str(current_container_name),
                    timeout=self._settings.ace_drain_timeout_seconds,
                )
            target_container_id = self._docker.create_container(
                name=target_container_name,
                image_tag=target_image_tag,
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
                container_id=target_container_id,
                lifecycle_state="starting",
                role="active",
                docker_labels_json=generation_metadata,
            )
            db.commit()
            self._docker.start_container(target_container_name)
            ready = await self._health.wait_until_ready(target_container_name)
            if not ready:
                raise OperationError("rollback candidate failed readiness", code="rollback_readiness_timeout")
        except Exception as exc:
            self._docker.cleanup_failed_container(
                target_container_name,
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
            self._runtime.update_runtime(db, user_id, status="failed")
            db.commit()
            if isinstance(exc, OperationError):
                raise
            raise OperationError(f"rollback recreate failed: {exc}", code="rollback_recreate_failed") from exc

        old_active = self._runtime.atomic_cutover(
            db,
            user_id=user_id,
            new_generation=int(target_generation),
            new_container_name=str(target_container_name),
            new_container_id=target_container_id,
            new_image_tag=str(target_image_tag),
        )
        self._runtime.update_generation(
            db,
            user_id=user_id,
            generation=int(target_generation),
            role="active",
            lifecycle_state="promoted",
            promoted_at="now()",
            health_status="healthy",
            health_summary=f"rollback success: {reason or 'manual rollback'}",
        )

        old_container_name = old_active.get("container_name")
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
            "old_generation": int(current_generation) if current_generation is not None else None,
            "new_generation": int(target_generation),
            "image_tag": target_image_tag,
            "recreated": True,
            "from_failed_deploy": bool(from_failed_deploy),
            "operation_id": operation_id,
        }
