from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ace_net_manager.settings import ManagerSettings
from ace_net_manager.utils import naming
from ace_net_manager.utils.errors import OperationError


class ProvisionService:
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

    async def cleanup_timed_out_provision(self, db: Session, *, user_id: str) -> str:
        runtime = self._runtime.get_runtime(db, user_id)
        if runtime is None:
            return "runtime_missing"

        generation = int(runtime.get("current_generation") or 1)
        container_name = runtime.get("active_container_name") or naming.container_name(user_id, generation)
        if container_name:
            self._docker.stop_and_remove_container(
                str(container_name),
                timeout=self._settings.ace_drain_timeout_seconds,
            )
        latest = self._runtime.get_latest_generation(db, user_id=user_id)
        if latest:
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=int(latest["generation"]),
                lifecycle_state="failed",
                role="failed",
                health_status="failed",
                health_summary="provision operation timed out",
            )
        self._runtime.update_runtime(db, user_id, status="failed", last_health_status="failed", desired_image_tag=None)
        db.commit()
        return "provision_timed_out_cleaned"

    async def provision_user(
        self,
        db: Session,
        *,
        user_id: str,
        image_tag: str,
        env_overrides: dict[str, str] | None,
        start_immediately: bool = True,
    ) -> dict[str, Any]:
        existing = self._runtime.get_runtime(db, user_id)
        if existing and existing.get("status") in {"active", "deploying", "restarting", "rollback_pending"}:
            raise OperationError("runtime already exists for user", code="runtime_exists")

        volume_name = naming.volume_name(user_id)
        network_name = self._settings.ace_docker_network
        generation = 1
        container_name = naming.container_name(user_id, generation)
        labels = naming.container_labels(user_id, generation, "active", image_tag)
        generation_metadata = naming.generation_metadata(
            user_id=user_id,
            generation=generation,
            role="active",
            image_tag=image_tag,
            env_overrides=env_overrides or {},
        )
        env = naming.runtime_env(
            user_id=user_id,
            generation=generation,
            role="active",
            env_overrides=env_overrides or {},
            internal_port=self._settings.ace_agent_internal_port,
            data_mount=self._settings.ace_agent_data_mount,
            skills_mount=self._settings.ace_runtime_skills_mount,
        )

        self._runtime.create_runtime(
            db,
            user_id=user_id,
            status="provisioning" if start_immediately else "disabled",
            network_name=network_name,
            volume_name=volume_name,
            desired_image_tag=image_tag,
        )
        if not start_immediately:
            db.commit()
            return {
                "user_id": user_id,
                "generation": None,
                "container_name": None,
                "container_id": None,
                "image_tag": image_tag,
            }
        self._runtime.create_generation(
            db,
            user_id=user_id,
            generation=generation,
            container_name=container_name,
            image_tag=image_tag,
            role="active",
            lifecycle_state="creating",
            docker_labels_json=generation_metadata,
        )
        db.commit()

        try:
            self._docker.ensure_network(network_name)
            self._docker.ensure_volume(
                volume_name,
                labels={
                    "ace.managed": "true",
                    "ace.user_id": user_id,
                    "ace.created_by": "ace-control-plane",
                },
            )
            self._docker.pull_image(image_tag)
            container_id = self._docker.create_container(
                name=container_name,
                image_tag=image_tag,
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
                generation=generation,
                lifecycle_state="starting",
                container_id=container_id,
                docker_labels_json=generation_metadata,
            )
            db.commit()

            self._docker.start_container(container_name)
            ready = await self._health.wait_until_ready(container_name)
            if not ready:
                raise OperationError("container failed readiness checks", code="readiness_timeout")

            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=generation,
                lifecycle_state="promoted",
                role="active",
                health_status="healthy",
                health_summary="new user runtime healthy",
                promoted_at="now()",
                started_at="now()",
            )
            self._runtime.update_runtime(
                db,
                user_id,
                status="active",
                current_generation=generation,
                active_container_name=container_name,
                active_container_id=container_id,
                active_image_tag=image_tag,
                previous_generation=None,
                previous_container_name=None,
                previous_container_id=None,
                previous_image_tag=None,
                desired_image_tag=None,
                last_health_status="healthy",
                last_health_checked_at="now()",
            )
            db.commit()
            return {
                "user_id": user_id,
                "generation": generation,
                "container_name": container_name,
                "container_id": container_id,
                "image_tag": image_tag,
            }
        except Exception as exc:
            self._docker.cleanup_failed_container(
                container_name,
                timeout=self._settings.ace_drain_timeout_seconds,
                preserve=self._settings.ace_preserve_failed_containers,
                preserved_name=naming.failed_container_name(user_id, generation),
            )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=generation,
                lifecycle_state="failed",
                role="failed",
                health_status="failed",
                health_summary=str(exc),
            )
            self._runtime.update_runtime(
                db,
                user_id,
                status="failed",
                last_health_status="failed",
            )
            db.commit()
            if isinstance(exc, OperationError):
                raise
            raise OperationError(f"failed to provision runtime: {exc}", code="provision_failed") from exc
