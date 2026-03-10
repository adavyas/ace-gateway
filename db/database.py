import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base
import scope_config as config

Base = declarative_base()


def _normalize_db_url(url: str) -> str:
    # Render/Heroku-style URLs sometimes come as `postgres://...` which SQLAlchemy
    # does not treat as a valid dialect. Normalize to `postgresql://...`.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]

    if url.startswith("sqlite:///"):
        path = url.replace("sqlite:///", "", 1)
        path = os.path.expanduser(path)
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        return f"sqlite:///{path}"
    return url


DATABASE_URL = _normalize_db_url(config.DATABASE_URL)

connect_args = {}
if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(DATABASE_URL, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


def _ensure_sqlite_auth_session_columns() -> None:
    """
    Backfill incremental schema changes for local SQLite DBs.
    `create_all()` creates missing tables but does not alter existing ones.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return

    required_columns = {
        "scope_access_token": "TEXT",
        "scope_refresh_token": "TEXT",
        "delivered_at": "DATETIME",
    }

    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(auth_sessions)").fetchall()
        if not rows:
            return

        existing = {row[1] for row in rows}
        for column_name, column_type in required_columns.items():
            if column_name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE auth_sessions ADD COLUMN {column_name} {column_type}"
                )


def _ensure_sqlite_job_queue_columns() -> None:
    """
    Backfill OpenClaw queue columns/indexes for existing local SQLite DBs.
    """
    if not DATABASE_URL.startswith("sqlite"):
        return

    required_columns = {
        "input": "TEXT",
        "worker_id": "TEXT",
        "lease_expires_at": "DATETIME",
    }

    with engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()
        if not rows:
            return

        existing = {row[1] for row in rows}
        for column_name, column_type in required_columns.items():
            if column_name not in existing:
                conn.exec_driver_sql(
                    f"ALTER TABLE jobs ADD COLUMN {column_name} {column_type}"
                )

        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_status_lease_expires_created_at "
            "ON jobs(status, lease_expires_at, created_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_input_target "
            "ON jobs(json_extract(input, '$.target'))"
        )


def _ensure_postgres_supabase_orchestration_schema() -> None:
    """
    Apply idempotent Supabase orchestration tables for Postgres deployments.
    """
    if DATABASE_URL.startswith("sqlite"):
        return

    schema_path = os.path.join(
        os.path.dirname(__file__), "migrations", "supabase_orchestration_schema.sql"
    )
    if not os.path.exists(schema_path):
        return

    with engine.begin() as conn:
        has_auth_users = bool(
            conn.exec_driver_sql("select to_regclass('auth.users') is not null").scalar()
        )
        if not has_auth_users:
            return

        with open(schema_path, "r", encoding="utf-8") as f:
            sql_text = f.read()

        # Execute one statement at a time for broad DBAPI compatibility.
        for statement in (stmt.strip() for stmt in sql_text.split(";")):
            if statement:
                conn.exec_driver_sql(statement)


def _ensure_postgres_extensions() -> None:
    """
    Ensure required Postgres extensions exist before creating tables that depend on
    custom types such as pgvector.
    """
    if DATABASE_URL.startswith("sqlite"):
        return

    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")


def init_db() -> None:
    from . import models  # noqa: F401
    _ensure_postgres_extensions()
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_auth_session_columns()
    _ensure_sqlite_job_queue_columns()
    _ensure_postgres_supabase_orchestration_schema()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
