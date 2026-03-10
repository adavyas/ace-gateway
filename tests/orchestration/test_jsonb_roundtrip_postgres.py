from __future__ import annotations

import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg2
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ace_net_manager.services.operation_service import OperationService
from ace_net_manager.services.runtime_service import RuntimeService


pytestmark = pytest.mark.integration


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _docker_available() -> bool:
    try:
        result = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return result.returncode == 0
    except Exception:
        return False


def _wait_for_postgres(dsn: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = psycopg2.connect(dsn)
            conn.close()
            return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError("postgres container did not become ready in time")


def test_jsonb_roundtrip_is_uuid_datetime_safe_real_postgres():
    if not _docker_available():
        pytest.skip("docker unavailable in test environment")

    port = _free_port()
    container_name = f"ace-jsonb-test-{port}"
    run_cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        container_name,
        "-e",
        "POSTGRES_PASSWORD=postgres",
        "-e",
        "POSTGRES_DB=postgres",
        "-p",
        f"{port}:5432",
        "postgres:16-alpine",
    ]
    container_id = subprocess.check_output(run_cmd, text=True).strip()

    dsn = f"postgresql://postgres:postgres@127.0.0.1:{port}/postgres"
    try:
        _wait_for_postgres(dsn)

        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("create extension if not exists pgcrypto")
                cur.execute("create schema if not exists auth")
                cur.execute(
                    """
                    create table if not exists auth.users (
                      id uuid primary key,
                      email text null
                    )
                    """
                )
                user_id = uuid4()
                cur.execute("insert into auth.users (id, email) values (%s, %s)", (str(user_id), "jsonb@test.local"))

                sql_path = Path(__file__).resolve().parents[2] / "db" / "migrations" / "supabase_orchestration_schema.sql"
                sql_text = sql_path.read_text(encoding="utf-8")
                cur.execute(sql_text)

        engine = create_engine(dsn, future=True)
        SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)
        runtime = RuntimeService()
        operation = OperationService(
            runtime_service=runtime,
            provision_service=SimpleNamespace(),
            deploy_service=SimpleNamespace(),
            rollback_service=SimpleNamespace(),
            restart_service=SimpleNamespace(),
        )

        db = SessionLocal()
        try:
            runtime.create_runtime(
                db,
                user_id=str(user_id),
                status="active",
                network_name="ace-net",
                volume_name="ace-data-u-user",
            )
            db.commit()

            op_id = operation.create_operation(
                db,
                user_id=str(user_id),
                operation_type="deploy",
                requested_by="admin",
                requested_image_tag="ace-agent:e2e",
                metadata={
                    "user_uuid": user_id,
                    "requested_at": datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
                    "tags": {"one", "two"},
                },
            )
            row = operation.get_operation(db, op_id)
            assert row is not None
            assert row["metadata"]["user_uuid"] == str(user_id)
            assert row["metadata"]["requested_at"] == "2026-01-01T12:00:00+00:00"
            assert sorted(row["metadata"]["tags"]) == ["one", "two"]

            runtime.create_generation(
                db,
                user_id=str(user_id),
                generation=1,
                container_name=f"ace-u-{user_id}-g1",
                image_tag="ace-agent:e2e",
                role="active",
                lifecycle_state="creating",
                docker_labels_json={
                    "user_id": user_id,
                    "seen_at": datetime(2026, 1, 2, 9, 30, 0, tzinfo=timezone.utc),
                    "flags": {"a", "b"},
                },
            )
            db.commit()
            gen = runtime.get_generation(db, user_id=str(user_id), generation=1)
            assert gen is not None
            assert gen["docker_labels_json"]["user_id"] == str(user_id)
            assert gen["docker_labels_json"]["seen_at"] == "2026-01-02T09:30:00+00:00"
            assert sorted(gen["docker_labels_json"]["flags"]) == ["a", "b"]
        finally:
            db.close()
            engine.dispose()
    finally:
        subprocess.run(["docker", "rm", "-f", container_id], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
