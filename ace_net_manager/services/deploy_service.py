from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ace_net_manager.settings import ManagerSettings
from ace_net_manager.utils import naming
from ace_net_manager.utils.errors import OperationError


class DeployService:
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
        self._rollback_service = None

    def set_rollback_service(self, rollback_service) -> None:
        self._rollback_service = rollback_service

    def _volume_labels(self, user_id: str) -> dict[str, str]:
        return {
            "ace.managed": "true",
            "ace.user_id": user_id,
            "ace.created_by": "ace-control-plane",
        }

    async def _restore_previous_active(
        self,
        db: Session,
        *,
        user_id: str,
        old_generation: int,
        old_container_name: str,
        old_image_tag: str,
        network_name: str,
        volume_name: str,
        env_overrides: dict[str, str] | None = None,
    ) -> bool:
        labels = naming.container_labels(user_id, old_generation, "active", old_image_tag)
        env = naming.runtime_env(
            user_id=user_id,
            generation=old_generation,
            role="active",
            env_overrides=env_overrides or {},
            internal_port=self._settings.ace_agent_internal_port,
            data_mount=self._settings.ace_agent_data_mount,
            skills_mount=self._settings.ace_runtime_skills_mount,
        )
        generation_metadata = naming.generation_metadata(
            user_id=user_id,
            generation=old_generation,
            role="active",
            image_tag=old_image_tag,
            env_overrides=env_overrides or {},
        )
        try:
            self._docker.ensure_volume(volume_name, labels=self._volume_labels(user_id))
            container_id = self._docker.create_container(
                name=old_container_name,
                image_tag=old_image_tag,
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
                generation=old_generation,
                role="active",
                lifecycle_state="starting",
                container_id=container_id,
                docker_labels_json=generation_metadata,
                health_status=None,
                health_summary="restoring previous generation after failed deploy",
            )
            db.commit()

            self._docker.start_container(old_container_name)
            ready = await self._health.wait_until_ready(old_container_name)
            if not ready:
                raise RuntimeError("restored previous generation failed readiness")

            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=old_generation,
                role="active",
                lifecycle_state="promoted",
                container_id=container_id,
                docker_labels_json=labels,
                health_status="healthy",
                health_summary="restored after failed deploy",
                promoted_at="now()",
            )
            self._runtime.update_runtime(
                db,
                user_id,
                status="active",
                current_generation=old_generation,
                active_container_name=old_container_name,
                active_container_id=container_id,
                active_image_tag=old_image_tag,
                desired_image_tag=None,
                last_health_status="healthy",
                last_health_checked_at="now()",
            )
            db.commit()
            return True
        except Exception:
            self._docker.stop_and_remove_container(
                old_container_name,
                timeout=self._settings.ace_drain_timeout_seconds,
            )
            return False

    async def cleanup_timed_out_deploy(self, db: Session, *, user_id: str, operation_id: str) -> str:
        runtime = self._runtime.get_runtime(db, user_id)
        if runtime is None:
            return "runtime_missing"

        current_generation = int(runtime.get("current_generation") or 0)
        latest = self._runtime.get_latest_generation(db, user_id=user_id)
        volume_name = runtime.get("volume_name") or naming.volume_name(user_id)
        network_name = runtime.get("network_name") or self._settings.ace_docker_network
        active_container_name = runtime.get("active_container_name")
        active_image_tag = runtime.get("active_image_tag")
        previous_row = self._runtime.get_generation(db, user_id=user_id, generation=current_generation) if current_generation else None
        previous_env_overrides = naming.env_overrides_from_generation(previous_row)

        if latest is not None and int(latest.get("generation") or 0) > current_generation:
            candidate_name = latest.get("container_name")
            if candidate_name:
                self._docker.stop_and_remove_container(
                    str(candidate_name),
                    timeout=self._settings.ace_drain_timeout_seconds,
                )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=int(latest["generation"]),
                role="failed",
                lifecycle_state="failed",
                health_status="failed",
                health_summary="operation timed out before cutover",
                )
            restored = False
            if active_container_name and not self._docker.container_exists(str(active_container_name)) and active_image_tag:
                restored = await self._restore_previous_active(
                    db,
                    user_id=user_id,
                    old_generation=current_generation,
                    old_container_name=str(active_container_name),
                    old_image_tag=str(active_image_tag),
                    network_name=str(network_name),
                    volume_name=str(volume_name),
                    env_overrides=previous_env_overrides,
                )
            if restored:
                return "pre_cutover_candidate_removed_and_previous_restored"
            self._runtime.update_runtime(
                db,
                user_id,
                status="active",
                desired_image_tag=None,
                last_health_status="healthy",
            )
            db.commit()
            return "pre_cutover_candidate_removed"

        self._runtime.update_runtime(db, user_id, status="failed", desired_image_tag=None, last_health_status="failed")
        db.commit()
        return "timeout_cleanup_unresolved"

    async def deploy_user(
        self,
        db: Session,
        *,
        user_id: str,
        image_tag: str,
        env_overrides: dict[str, str] | None,
        keep_previous_warm_seconds: int | None,
        operation_id: str,
    ) -> dict[str, Any]:
        runtime = self._runtime.get_runtime(db, user_id)
        if runtime is None:
            raise OperationError("runtime not found", code="runtime_missing")
        if runtime.get("status") != "active":
            raise OperationError("runtime must be active before deploy", code="invalid_runtime_state")
        current_generation = int(runtime.get("current_generation") or 0)
        if current_generation < 1:
            raise OperationError("runtime has no active generation", code="invalid_runtime_state")

        volume_name = runtime.get("volume_name") or naming.volume_name(user_id)
        network_name = runtime.get("network_name") or self._settings.ace_docker_network
        current_container_name = runtime.get("active_container_name") or naming.container_name(user_id, current_generation)
        current_image_tag = runtime.get("active_image_tag")
        new_generation = current_generation + 1
        candidate_name = naming.container_name(user_id, new_generation)
        active_labels = naming.container_labels(user_id, new_generation, "active", image_tag)
        active_env_overrides = env_overrides or {}
        active_env = naming.runtime_env(
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
            image_tag=image_tag,
            env_overrides=active_env_overrides,
        )
        self._runtime.update_runtime(db, user_id, status="deploying", desired_image_tag=image_tag)
        self._runtime.create_generation(
            db,
            user_id=user_id,
            generation=new_generation,
            container_name=candidate_name,
            image_tag=image_tag,
            role="active",
            lifecycle_state="creating",
            docker_labels_json=generation_metadata,
        )
        db.commit()

        old_removed = False
        try:
            self._docker.ensure_volume(volume_name, labels=self._volume_labels(user_id))
            if current_container_name and current_image_tag:
                self._docker.assert_container_matches(
                    str(current_container_name),
                    image_tag=str(current_image_tag),
                    network_name=str(network_name),
                    volume_name=str(volume_name),
                    labels=naming.container_labels(user_id, current_generation, "active", str(current_image_tag)),
                )
            self._docker.pull_image(image_tag)
            self._docker.stop_and_remove_container(
                str(current_container_name),
                timeout=self._settings.ace_drain_timeout_seconds,
            )
            old_removed = True

            candidate_container_id = self._docker.create_container(
                name=candidate_name,
                image_tag=image_tag,
                network_name=network_name,
                volume_name=volume_name,
                labels=active_labels,
                environment=active_env,
                internal_port=self._settings.ace_agent_internal_port,
                global_skills_dir=self._settings.ace_global_skills_dir,
                runtime_skills_mount=self._settings.ace_runtime_skills_mount,
            )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=new_generation,
                lifecycle_state="starting",
                role="active",
                container_id=candidate_container_id,
                docker_labels_json=generation_metadata,
            )
            db.commit()
            self._docker.start_container(candidate_name)
            active_ready = await self._health.wait_until_ready(candidate_name)
            if not active_ready:
                raise OperationError("candidate failed readiness checks", code="readiness_timeout")

            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=new_generation,
                lifecycle_state="healthy",
                role="active",
                health_status="healthy",
                health_summary="active candidate healthy",
                started_at="now()",
            )
            old_active = self._runtime.atomic_cutover(
                db,
                user_id=user_id,
                new_generation=new_generation,
                new_container_name=candidate_name,
                new_container_id=candidate_container_id,
                new_image_tag=image_tag,
            )
            self._runtime.update_generation(
                db,
                user_id=user_id,
                generation=new_generation,
                role="active",
                lifecycle_state="promoted",
                promoted_at="now()",
            )
            if old_active.get("generation"):
                self._runtime.update_generation(
                    db,
                    user_id=user_id,
                    generation=int(old_active["generation"]),
                    role="retired",
                    lifecycle_state="removed",
                    retired_at="now()",
                )
            db.commit()
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
                "active_container_name": candidate_name,
                "active_image_tag": image_tag,
            }
        except Exception as exc:
            self._docker.cleanup_failed_container(
                candidate_name,
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
            restored = False
            if old_removed and current_container_name and current_image_tag:
                restored = await self._restore_previous_active(
                    db,
                    user_id=user_id,
                    old_generation=current_generation,
                    old_container_name=str(current_container_name),
                    old_image_tag=str(current_image_tag),
                    network_name=str(network_name),
                    volume_name=str(volume_name),
                )
            if restored:
                self._runtime.update_runtime(
                    db,
                    user_id,
                    status="active",
                    desired_image_tag=None,
                    last_health_status="healthy",
                    last_health_checked_at="now()",
                )
            else:
                self._runtime.update_runtime(
                    db,
                    user_id,
                    status="failed",
                    desired_image_tag=None,
                    last_health_status="failed",
                )
            db.commit()
            if isinstance(exc, OperationError):
                raise
            raise OperationError(f"deploy failed: {exc}", code="deploy_failed") from exc
