import datetime as dt
import json
import hashlib
import secrets
import re
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import select, delete
from sqlalchemy.exc import IntegrityError

from . import models
import scope_config as config


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_user(db: Session, email: str, password_hash: Optional[str], full_name: Optional[str], provider: str) -> models.User:
    user = models.User(
        id=secrets.token_urlsafe(16),
        email=email.lower().strip(),
        password_hash=password_hash,
        full_name=full_name,
        provider=provider,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def get_user_by_email(db: Session, email: str) -> Optional[models.User]:
    stmt = select(models.User).where(models.User.email == email.lower().strip())
    return db.execute(stmt).scalar_one_or_none()


def get_user_by_id(db: Session, user_id: str) -> Optional[models.User]:
    return db.get(models.User, user_id)


def ensure_user_exists(
    db: Session,
    user_id: str,
    *,
    email_hint: Optional[str] = None,
    full_name: Optional[str] = None,
    provider: str = "password",
) -> Optional[models.User]:
    """
    Ensure a DB-backed user row exists for the given id.

    In production this is intentionally conservative and does not auto-create users.
    In development it can bootstrap a local user record (e.g. dev_user) to satisfy FK constraints.
    """
    existing = get_user_by_id(db, user_id)
    if existing:
        return existing

    if email_hint:
        by_email = get_user_by_email(db, email_hint)
        if by_email:
            return by_email

    is_dev = config._ENV == "development" or config.SECRET_KEY == "dev-insecure-secret"
    if not is_dev:
        return None

    local_part = re.sub(r"[^a-zA-Z0-9_.-]+", "-", (user_id or "dev_user")).strip("-").lower() or "dev_user"
    email = (email_hint or f"{local_part}@scope.local").lower().strip()

    user = models.User(
        id=user_id,
        email=email,
        password_hash=None,
        full_name=full_name or "Developer",
        provider=provider,
        is_active=True,
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
        return user
    except IntegrityError:
        db.rollback()
        # Handle races or email conflicts gracefully.
        existing = get_user_by_id(db, user_id)
        if existing:
            return existing
        by_email = get_user_by_email(db, email)
        if by_email:
            return by_email
        return None


def create_refresh_token(db: Session, user_id: str, token: str) -> models.RefreshToken:
    expires_at = dt.datetime.utcnow() + dt.timedelta(days=config.REFRESH_TOKEN_EXPIRE_DAYS)
    token_hash = _hash_token(token)
    entry = models.RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def revoke_refresh_token(db: Session, token: str) -> bool:
    token_hash = _hash_token(token)
    stmt = select(models.RefreshToken).where(models.RefreshToken.token_hash == token_hash)
    entry = db.execute(stmt).scalar_one_or_none()
    if not entry:
        return False
    entry.revoked_at = dt.datetime.utcnow()
    db.add(entry)
    db.commit()
    return True


def rotate_refresh_token(db: Session, token: str) -> Optional[models.User]:
    token_hash = _hash_token(token)
    stmt = select(models.RefreshToken).where(models.RefreshToken.token_hash == token_hash)
    entry = db.execute(stmt).scalar_one_or_none()
    if not entry:
        return None
    if entry.revoked_at or entry.expires_at < dt.datetime.utcnow():
        return None
    user = get_user_by_id(db, entry.user_id)
    entry.revoked_at = dt.datetime.utcnow()
    db.add(entry)
    db.commit()
    return user


def add_chat_message(db: Session, user_id: str, session_id: Optional[str], role: str, content: str) -> None:
    def _insert_once() -> None:
        msg = models.ChatMessage(
            user_id=user_id,
            session_id=session_id,
            role=role,
            content=content,
        )
        db.add(msg)
        db.commit()

    try:
        _insert_once()
        return
    except IntegrityError as exc:
        db.rollback()
        err = str(exc).lower()
        fk_missing_user = (
            "chat_messages_user_id_fkey" in err
            or ("foreign key" in err and "user_id" in err and "chat_messages" in err)
        )
        if not fk_missing_user:
            raise

    # Dev-mode recovery path: ensure local user row exists, then retry insert once.
    ensured = ensure_user_exists(
        db,
        user_id,
        email_hint=f"{(user_id or 'dev_user').lower()}@scope.local",
        full_name="Developer",
    )
    if not ensured:
        raise
    _insert_once()


def get_chat_history(db: Session, user_id: str, session_id: Optional[str], limit: int) -> list[dict]:
    stmt = select(models.ChatMessage).where(models.ChatMessage.user_id == user_id)
    if session_id:
        stmt = stmt.where(models.ChatMessage.session_id == session_id)
    stmt = stmt.order_by(models.ChatMessage.created_at.desc()).limit(limit)
    rows = list(reversed(db.execute(stmt).scalars().all()))
    return [{"role": r.role, "content": r.content} for r in rows]


def get_or_create_consent(db: Session, user_id: str) -> models.CaptureConsent:
    consent = db.get(models.CaptureConsent, user_id)
    if consent:
        return consent
    consent = models.CaptureConsent(user_id=user_id, opt_in=True)
    db.add(consent)
    db.commit()
    db.refresh(consent)
    return consent


def set_consent(db: Session, user_id: str, opt_in: bool) -> models.CaptureConsent:
    consent = get_or_create_consent(db, user_id)
    consent.opt_in = opt_in
    db.add(consent)
    db.commit()
    db.refresh(consent)
    return consent


def get_permission_paths(db: Session, user_id: str) -> list[str]:
    stmt = select(models.PermissionGrant).where(models.PermissionGrant.user_id == user_id)
    return [row.path for row in db.execute(stmt).scalars().all()]


def grant_permission(db: Session, user_id: str, path: str) -> None:
    entry = models.PermissionGrant(user_id=user_id, path=path)
    db.add(entry)
    db.commit()


def revoke_permission(db: Session, user_id: str, path: str) -> None:
    stmt = delete(models.PermissionGrant).where(
        models.PermissionGrant.user_id == user_id,
        models.PermissionGrant.path == path,
    )
    db.execute(stmt)
    db.commit()


def enqueue_vector_replication(
    db: Session,
    user_id: str,
    record_id: str,
    content: str,
    metadata: dict,
    vector: list[float],
) -> None:
    entry = models.VectorReplicationTask(
        user_id=user_id,
        record_id=record_id,
        content=content,
        metadata_json=json.dumps(metadata, ensure_ascii=True),
        vector_json=json.dumps(vector),
        status="pending",
    )
    db.add(entry)
    db.commit()


def claim_vector_tasks(db: Session, limit: int = 25) -> list[models.VectorReplicationTask]:
    stmt = (
        select(models.VectorReplicationTask)
        .where(models.VectorReplicationTask.status == "pending")
        .limit(limit)
    )
    tasks = db.execute(stmt).scalars().all()
    for task in tasks:
        task.status = "in_progress"
        task.updated_at = dt.datetime.utcnow()
        db.add(task)
    db.commit()
    return tasks


def complete_vector_task(db: Session, task_id: int, error: Optional[str] = None) -> None:
    stmt = select(models.VectorReplicationTask).where(models.VectorReplicationTask.id == task_id)
    task = db.execute(stmt).scalar_one_or_none()
    if not task:
        return
    task.status = "failed" if error else "done"
    task.error = error
    task.updated_at = dt.datetime.utcnow()
    db.add(task)
    db.commit()
