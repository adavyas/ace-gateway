from __future__ import annotations

from datetime import datetime
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from ace_net_manager.utils.errors import OperationError
from ace_net_manager.utils.json_safe import to_jsonable


class OperationService:
    def __init__(
        self,
        *,
        runtime_service,
        provision_service,
        deploy_service,
        rollback_service,
        restart_service,
    ) -> None:
        self._runtime = runtime_service
        self._provision = provision_service
        self._deploy = deploy_service
        self._rollback = rollback_service
        self._restart = restart_service

    def has_inflight_operation(self, db: Session, user_id: str) -> bool:
        row = db.execute(
            text(
                """
                select 1
                from orchestration.runtime_operations
                where user_id = :user_id
                  and status in ('queued', 'running')
                limit 1
                """
            ),
            {"user_id": user_id},
        ).first()
        return row is not None

    def create_operation(
        self,
        db: Session,
        *,
        user_id: str,
        operation_type: str,
        requested_by: str,
        requested_image_tag: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        operation_id = str(uuid4())
        db.execute(
            text(
                """
                insert into orchestration.runtime_operations (
                    operation_id,
                    user_id,
                    operation_type,
                    requested_by,
                    requested_image_tag,
                    status,
                    step,
                    metadata,
                    created_at,
                    updated_at
                ) values (
                    :operation_id,
                    :user_id,
                    :operation_type,
                    :requested_by,
                    :requested_image_tag,
                    'queued',
                    'queued',
                    cast(:metadata as jsonb),
                    now(),
                    now()
                )
                """
            ),
            {
                "operation_id": operation_id,
                "user_id": user_id,
                "operation_type": operation_type,
                "requested_by": requested_by,
                "requested_image_tag": requested_image_tag,
                "metadata": json.dumps(to_jsonable(metadata or {}), ensure_ascii=True),
            },
        )
        self._runtime.set_last_operation(db, user_id=user_id, operation_id=operation_id)
        db.commit()
        return operation_id

    def get_operation(self, db: Session, operation_id: str) -> dict[str, Any] | None:
        row = db.execute(
            text(
                """
                select *
                from orchestration.runtime_operations
                where operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id},
        ).mappings().first()
        return dict(row) if row else None

    def set_step(self, db: Session, operation_id: str, step: str) -> None:
        db.execute(
            text(
                """
                update orchestration.runtime_operations
                set step = :step, updated_at = now()
                where operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id, "step": step},
        )
        db.commit()

    def mark_running(self, db: Session, operation_id: str, step: str = "running") -> None:
        db.execute(
            text(
                """
                update orchestration.runtime_operations
                set status = 'running',
                    step = :step,
                    started_at = now(),
                    updated_at = now()
                where operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id, "step": step},
        )
        db.commit()

    def mark_succeeded(
        self,
        db: Session,
        operation_id: str,
        *,
        old_generation: int | None = None,
        new_generation: int | None = None,
    ) -> None:
        db.execute(
            text(
                """
                update orchestration.runtime_operations
                set status = 'succeeded',
                    step = 'completed',
                    old_generation = :old_generation,
                    new_generation = :new_generation,
                    finished_at = now(),
                    updated_at = now()
                where operation_id = :operation_id
                """
            ),
            {
                "operation_id": operation_id,
                "old_generation": old_generation,
                "new_generation": new_generation,
            },
        )
        db.commit()

    def mark_rolled_back(self, db: Session, operation_id: str, message: str) -> None:
        db.execute(
            text(
                """
                update orchestration.runtime_operations
                set status = 'rolled_back',
                    step = 'rolled_back',
                    error_code = 'rolled_back',
                    error_message = :message,
                    finished_at = now(),
                    updated_at = now()
                where operation_id = :operation_id
                """
            ),
            {"operation_id": operation_id, "message": message},
        )
        db.commit()

    def mark_failed(self, db: Session, operation_id: str, *, error_code: str, error_message: str) -> None:
        db.execute(
            text(
                """
                update orchestration.runtime_operations
                set status = 'failed',
                    step = 'failed',
                    error_code = :error_code,
                    error_message = :error_message,
                    finished_at = now(),
                    updated_at = now()
                where operation_id = :operation_id
                """
            ),
            {
                "operation_id": operation_id,
                "error_code": error_code,
                "error_message": error_message,
            },
        )
        db.commit()

    def fail_stale_inflight_operations(self, db: Session, *, stale_minutes: int) -> int:
        stale_minutes = max(1, int(stale_minutes))
        is_postgres = False
        try:
            is_postgres = str(db.bind.dialect.name).startswith("postgresql")  # type: ignore[union-attr]
        except Exception:
            is_postgres = False

        if is_postgres:
            rows = db.execute(
                text(
                    """
                    update orchestration.runtime_operations
                    set status = 'failed',
                        step = 'failed',
                        error_code = 'stale_manager_restart',
                        error_message = 'operation became stale while manager was offline',
                        finished_at = now(),
                        updated_at = now()
                    where status in ('queued', 'running')
                      and created_at < (now() - (:stale_minutes * interval '1 minute'))
                    returning operation_id
                    """
                ),
                {"stale_minutes": stale_minutes},
            ).fetchall()
            db.commit()
            return len(rows)

        rows = db.execute(
            text(
                """
                update orchestration.runtime_operations
                set status = 'failed',
                    step = 'failed',
                    error_code = 'stale_manager_restart',
                    error_message = 'operation became stale while manager was offline',
                    finished_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                where status in ('queued', 'running')
                  and created_at < datetime('now', :age)
                """
            ),
            {"age": f"-{stale_minutes} minutes"},
        )
        db.commit()
        return int(rows.rowcount or 0)

    async def handle_timeout_cleanup(self, db: Session, operation: dict[str, Any]) -> str:
        op_type = str(operation.get("operation_type") or "")
        user_id = str(operation.get("user_id") or "")
        if not user_id:
            return "skipped"

        if op_type in {"deploy", "restart"}:
            return await self._deploy.cleanup_timed_out_deploy(db, user_id=user_id, operation_id=str(operation["operation_id"]))
        if op_type == "new_user":
            return await self._provision.cleanup_timed_out_provision(db, user_id=user_id)
        return "skipped"

    async def execute(self, db: Session, operation: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(operation["operation_id"])
        user_id = str(operation["user_id"])
        metadata = to_jsonable(operation.get("metadata") or {})
        op_type = str(operation["operation_type"])

        if op_type == "new_user":
            self.set_step(db, operation_id, "provision")
            image_tag = str(metadata.get("image_tag") or "").strip() or None
            env_overrides = metadata.get("env_overrides") or {}
            result = await self._provision.provision_user(
                db,
                user_id=user_id,
                image_tag=image_tag or metadata.get("default_image_tag") or operation.get("requested_image_tag"),
                env_overrides=env_overrides,
                start_immediately=bool(metadata.get("start_immediately", True)),
            )
            return result
        if op_type == "deploy":
            self.set_step(db, operation_id, "deploy")
            result = await self._deploy.deploy_user(
                db,
                user_id=user_id,
                image_tag=str(operation.get("requested_image_tag") or metadata.get("image_tag")),
                env_overrides=metadata.get("env_overrides") or {},
                keep_previous_warm_seconds=metadata.get("keep_previous_warm_seconds"),
                operation_id=operation_id,
            )
            return result
        if op_type == "rollback":
            self.set_step(db, operation_id, "rollback")
            return await self._rollback.rollback_user(
                db,
                user_id=user_id,
                reason=metadata.get("reason"),
                operation_id=operation_id,
            )
        if op_type == "restart":
            self.set_step(db, operation_id, "restart")
            return await self._restart.restart_user(
                db,
                user_id=user_id,
                mode=str(metadata.get("mode") or "safe"),
                operation_id=operation_id,
                requested_by=str(operation.get("requested_by") or "admin"),
            )
        raise OperationError(f"unsupported operation type: {op_type}", code="unsupported_operation")

    @staticmethod
    def serialize_operation(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for key, value in list(out.items()):
            if isinstance(value, datetime):
                out[key] = value.isoformat()
            elif value is None:
                out[key] = None
        return out
