from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ace_net_manager.utils.json_safe import to_jsonable


def _now_sql() -> str:
    return "now()"


class RuntimeService:
    def get_runtime(self, db: Session, user_id: str) -> dict[str, Any] | None:
        row = db.execute(
            text(
                """
                select *
                from orchestration.user_runtimes
                where user_id = :user_id
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
        return dict(row) if row else None

    def create_runtime(
        self,
        db: Session,
        *,
        user_id: str,
        status: str,
        network_name: str,
        volume_name: str,
        desired_image_tag: str | None = None,
    ) -> None:
        db.execute(
            text(
                f"""
                insert into orchestration.user_runtimes (
                    user_id, status, network_name, volume_name, desired_image_tag, created_at, updated_at
                ) values (
                    :user_id, :status, :network_name, :volume_name, :desired_image_tag, {_now_sql()}, {_now_sql()}
                )
                on conflict (user_id) do update set
                    status = excluded.status,
                    network_name = excluded.network_name,
                    volume_name = excluded.volume_name,
                    desired_image_tag = excluded.desired_image_tag,
                    updated_at = {_now_sql()}
                """
            ),
            {
                "user_id": user_id,
                "status": status,
                "network_name": network_name,
                "volume_name": volume_name,
                "desired_image_tag": desired_image_tag,
            },
        )

    def update_runtime(self, db: Session, user_id: str, **fields: Any) -> None:
        if not fields:
            return
        assignments = []
        params: dict[str, Any] = {"user_id": user_id}
        for key, value in fields.items():
            if value == "now()":
                assignments.append(f"{key} = now()")
                continue
            assignments.append(f"{key} = :{key}")
            params[key] = value
        assignments.append(f"updated_at = {_now_sql()}")
        sql = f"""
            update orchestration.user_runtimes
            set {", ".join(assignments)}
            where user_id = :user_id
        """
        db.execute(text(sql), params)

    def set_last_operation(self, db: Session, *, user_id: str, operation_id: str) -> None:
        self.update_runtime(db, user_id, last_operation_id=operation_id)

    def create_generation(
        self,
        db: Session,
        *,
        user_id: str,
        generation: int,
        container_name: str,
        image_tag: str,
        role: str,
        lifecycle_state: str,
        container_id: str | None = None,
        docker_labels_json: dict[str, Any] | None = None,
        rollback_to_image_tag: str | None = None,
    ) -> None:
        db.execute(
            text(
                f"""
                insert into orchestration.runtime_generations (
                    user_id,
                    generation,
                    container_name,
                    container_id,
                    image_tag,
                    role,
                    lifecycle_state,
                    docker_labels_json,
                    rollback_to_image_tag,
                    created_at,
                    updated_at
                ) values (
                    :user_id,
                    :generation,
                    :container_name,
                    :container_id,
                    :image_tag,
                    :role,
                    :lifecycle_state,
                    cast(:docker_labels_json as jsonb),
                    :rollback_to_image_tag,
                    {_now_sql()},
                    {_now_sql()}
                )
                """
            ),
            {
                "user_id": user_id,
                "generation": int(generation),
                "container_name": container_name,
                "container_id": container_id,
                "image_tag": image_tag,
                "role": role,
                "lifecycle_state": lifecycle_state,
                "docker_labels_json": json.dumps(to_jsonable(docker_labels_json or {}), ensure_ascii=True),
                "rollback_to_image_tag": rollback_to_image_tag,
            },
        )

    def get_generation(self, db: Session, *, user_id: str, generation: int) -> dict[str, Any] | None:
        row = db.execute(
            text(
                """
                select *
                from orchestration.runtime_generations
                where user_id = :user_id and generation = :generation
                """
            ),
            {"user_id": user_id, "generation": int(generation)},
        ).mappings().first()
        return dict(row) if row else None

    def get_latest_generation(self, db: Session, *, user_id: str) -> dict[str, Any] | None:
        row = db.execute(
            text(
                """
                select *
                from orchestration.runtime_generations
                where user_id = :user_id
                order by generation desc
                limit 1
                """
            ),
            {"user_id": user_id},
        ).mappings().first()
        return dict(row) if row else None

    def update_generation(self, db: Session, *, user_id: str, generation: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = []
        params: dict[str, Any] = {"user_id": user_id, "generation": int(generation)}
        for key, value in fields.items():
            if value == "now()":
                assignments.append(f"{key} = now()")
                continue
            if key == "docker_labels_json":
                assignments.append(f"{key} = cast(:{key} as jsonb)")
                params[key] = json.dumps(to_jsonable(value or {}), ensure_ascii=True)
            else:
                assignments.append(f"{key} = :{key}")
                params[key] = value
        assignments.append(f"updated_at = {_now_sql()}")
        sql = f"""
            update orchestration.runtime_generations
            set {", ".join(assignments)}
            where user_id = :user_id and generation = :generation
        """
        db.execute(text(sql), params)

    def atomic_cutover(
        self,
        db: Session,
        *,
        user_id: str,
        new_generation: int,
        new_container_name: str,
        new_container_id: str | None,
        new_image_tag: str,
    ) -> dict[str, Any]:
        runtime = self.get_runtime(db, user_id)
        if runtime is None:
            raise RuntimeError("runtime not found")

        old_active = {
            "generation": runtime.get("current_generation"),
            "container_name": runtime.get("active_container_name"),
            "container_id": runtime.get("active_container_id"),
            "image_tag": runtime.get("active_image_tag"),
        }
        self.update_runtime(
            db,
            user_id,
            previous_generation=runtime.get("current_generation"),
            previous_container_name=runtime.get("active_container_name"),
            previous_container_id=runtime.get("active_container_id"),
            previous_image_tag=runtime.get("active_image_tag"),
            current_generation=int(new_generation),
            active_container_name=new_container_name,
            active_container_id=new_container_id,
            active_image_tag=new_image_tag,
            status="deploying",
        )
        return old_active
