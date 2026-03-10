from __future__ import annotations

from pathlib import Path


def test_supabase_orchestration_schema_includes_runtime_control_plane_tables():
    sql_path = Path(__file__).resolve().parents[2] / "db" / "migrations" / "supabase_orchestration_schema.sql"
    sql = sql_path.read_text(encoding="utf-8")

    assert "create table if not exists orchestration.user_runtimes" in sql
    assert "create table if not exists orchestration.runtime_generations" in sql
    assert "create table if not exists orchestration.runtime_operations" in sql

    assert "references auth.users(id) on delete cascade" in sql
    assert "operation_type in ('new_user', 'deploy', 'rollback', 'restart')" in sql
    assert "requested_by in ('system', 'admin', 'gateway')" in sql
    assert "status in ('queued', 'running', 'succeeded', 'failed', 'rolled_back', 'cancelled')" in sql
    assert "last_operation_id uuid null references orchestration.runtime_operations(operation_id) on delete set null" in sql


def test_schema_has_required_indexes():
    sql_path = Path(__file__).resolve().parents[2] / "db" / "migrations" / "supabase_orchestration_schema.sql"
    sql = sql_path.read_text(encoding="utf-8")

    assert "ix_orchestration_user_runtimes_status" in sql
    assert "ix_orchestration_runtime_generations_user_id_lifecycle_state" in sql
    assert "ix_orchestration_runtime_generations_user_id_role" in sql
    assert "ix_orchestration_runtime_operations_user_id_created_at" in sql
    assert "ix_orchestration_runtime_operations_status_created_at" in sql

