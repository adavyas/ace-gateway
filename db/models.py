import datetime as dt
import time
from sqlalchemy import (
    Column,
    String,
    DateTime,
    Boolean,
    Integer,
    Text,
    ForeignKey,
    JSON,
    Index,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from .database import Base

try:
    from pgvector.sqlalchemy import Vector
except Exception:  # pragma: no cover
    Vector = None


def _vector_384_type():
    # Keep SQLite dev working by storing embeddings as TEXT (JSON) locally.
    if Vector is None:
        return Text()
    try:
        return Vector(384).with_variant(Text(), "sqlite")
    except Exception:
        return Text()


VECTOR_384 = _vector_384_type()


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=True)
    full_name = Column(String, nullable=True)
    provider = Column(String, nullable=False, default="password")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    refresh_tokens = relationship("RefreshToken", back_populates="user")


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    token_hash = Column(String, nullable=False, unique=True)
    revoked_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    user = relationship("User", back_populates="refresh_tokens")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    session_id = Column(String, index=True, nullable=True)
    role = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class PermissionGrant(Base):
    __tablename__ = "permission_grants"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    path = Column(Text, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class CaptureConsent(Base):
    __tablename__ = "capture_consents"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    opt_in = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class VectorReplicationTask(Base):
    __tablename__ = "vector_replication_tasks"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    record_id = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=False)
    vector_json = Column(Text, nullable=False)
    status = Column(String, default="pending", index=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class AuthSession(Base):
    """
    Short-lived desktop OAuth session for Google linking (polling-based).
    """

    __tablename__ = "auth_sessions"

    id = Column(String, primary_key=True, index=True)
    status = Column(String, default="PENDING", index=True)
    state = Column(String, unique=True, nullable=False, index=True)
    pkce_verifier = Column(Text, nullable=False)
    redirect_uri = Column(Text, nullable=False)
    scopes = Column(Text, nullable=False, default="")
    expires_at_epoch = Column(Integer, nullable=False, index=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=True, index=True)
    error = Column(Text, nullable=True)
    scope_access_token = Column(Text, nullable=True)
    scope_refresh_token = Column(Text, nullable=True)
    delivered_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    @property
    def is_expired(self) -> bool:
        try:
            # Compare epoch seconds to epoch seconds to avoid local-time offset bugs.
            return int(self.expires_at_epoch) <= int(time.time())
        except Exception:
            return True


class OAuthGoogleToken(Base):
    """
    Encrypted Google refresh token vault (envelope encryption).
    """

    __tablename__ = "oauth_google_tokens"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)

    google_sub = Column(String, nullable=True)
    google_email = Column(String, nullable=True)

    encrypted_refresh_token = Column(Text, nullable=True)
    refresh_token_encrypted_dek = Column(Text, nullable=True)
    dek_nonce = Column(Text, nullable=True)
    token_nonce = Column(Text, nullable=True)

    scopes = Column(Text, nullable=False, default="")
    revoked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class TwilioPhoneLink(Base):
    __tablename__ = "twilio_phone_links"

    phone_number = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    auth_session_id = Column(String, ForeignKey("auth_sessions.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class TwilioPendingLink(Base):
    __tablename__ = "twilio_pending_links"

    phone_number = Column(String, primary_key=True)
    auth_session_id = Column(String, ForeignKey("auth_sessions.id"), nullable=False, unique=True, index=True)
    auth_url = Column(Text, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class MessagingLink(Base):
    __tablename__ = "messaging_links"

    provider = Column(String, primary_key=True)
    address = Column(String, primary_key=True)
    user_id = Column(String, ForeignKey("users.id"), nullable=False, index=True)
    channel = Column(String, nullable=False, index=True)
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    __table_args__ = (
        Index("ix_messaging_links_user_id_provider", "user_id", "provider"),
    )


class Device(Base):
    __tablename__ = "devices"

    id = Column(String, primary_key=True, index=True)  # device_id
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    public_key = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    last_seen_at = Column(DateTime, default=dt.datetime.utcnow)


class Consent(Base):
    __tablename__ = "consents"

    user_id = Column(String, ForeignKey("users.id"), primary_key=True)
    capture_enabled = Column(Boolean, default=True, nullable=False)
    data_share_enabled = Column(Boolean, default=False, nullable=False)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)
    version = Column(String, default="v1", nullable=False)


class CaptureSession(Base):
    __tablename__ = "capture_sessions"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    device_id = Column(String, index=True, nullable=False)
    started_at = Column(DateTime, default=dt.datetime.utcnow, nullable=False)
    ended_at = Column(DateTime, nullable=True)
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)


class CaptureEvent(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    device_id = Column(String, index=True, nullable=False)
    capture_session_id = Column(String, ForeignKey("capture_sessions.id"), index=True, nullable=False)
    event_id = Column(String, nullable=False)
    ts_ms = Column(Integer, index=True, nullable=False)
    type = Column(String, index=True, nullable=False)
    payload = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_events_user_event_id"),
        {"sqlite_autoincrement": True},
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(String, primary_key=True, index=True)  # artifact_id
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    device_id = Column(String, index=True, nullable=False)
    capture_session_id = Column(String, ForeignKey("capture_sessions.id"), index=True, nullable=False)
    ts_ms = Column(Integer, index=True, nullable=False)
    kind = Column(String, index=True, nullable=False)
    storage_path = Column(Text, nullable=False)
    sha256 = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    mime = Column(String, nullable=False)
    status = Column(String, index=True, default="PRESIGNED")
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class ArtifactFeature(Base):
    __tablename__ = "artifact_features"

    artifact_id = Column(String, ForeignKey("artifacts.id"), primary_key=True)
    ocr_text = Column(Text, nullable=True)
    embedding = Column(VECTOR_384, nullable=True)
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class Memory(Base):
    __tablename__ = "memories"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    device_id = Column(String, index=True, nullable=True)
    type = Column(String, index=True, nullable=False)
    text = Column(Text, nullable=False)
    embedding = Column(VECTOR_384, nullable=True)
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class GraphNode(Base):
    __tablename__ = "graph_nodes"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    node_type = Column(String, index=True, nullable=False)
    name = Column(String, index=True, nullable=False)
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class GraphEdge(Base):
    __tablename__ = "graph_edges"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    src_id = Column(String, index=True, nullable=False)
    dst_id = Column(String, index=True, nullable=False)
    edge_type = Column(String, index=True, nullable=False)
    weight = Column(Integer, default=1, nullable=False)
    valid_from = Column(DateTime, nullable=True)
    valid_to = Column(DateTime, nullable=True)
    metadata_json = Column("metadata", JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class Pipeline(Base):
    __tablename__ = "pipelines"

    id = Column(String, primary_key=True, index=True)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    created_from_capture_session_id = Column(String, ForeignKey("capture_sessions.id"), nullable=True, index=True)
    spec = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=dt.datetime.utcnow)


class Run(Base):
    __tablename__ = "runs"

    id = Column(String, primary_key=True, index=True)
    pipeline_id = Column(String, ForeignKey("pipelines.id"), index=True, nullable=False)
    user_id = Column(String, ForeignKey("users.id"), index=True, nullable=False)
    status = Column(String, index=True, nullable=False, default="QUEUED")
    iteration = Column(Integer, default=0, nullable=False)
    scores = Column(JSON, default=dict, nullable=False)
    logs_path = Column(Text, default="", nullable=False)
    patch_path = Column(Text, default="", nullable=False)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String, index=True, nullable=False)
    input_json = Column("input", JSON, default=dict, nullable=True)
    payload = Column(JSON, default=dict, nullable=False)
    status = Column(String, index=True, nullable=False, default="pending")
    worker_id = Column(String, index=True, nullable=True)
    lease_expires_at = Column(DateTime, index=True, nullable=True)
    attempts = Column(Integer, default=0, nullable=False)
    run_after = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=dt.datetime.utcnow)
    updated_at = Column(DateTime, default=dt.datetime.utcnow, onupdate=dt.datetime.utcnow)

    __table_args__ = (
        Index("ix_jobs_status_lease_expires_created_at", "status", "lease_expires_at", "created_at"),
    )


class JobLog(Base):
    __tablename__ = "job_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False, index=True)
    ts = Column(DateTime, default=dt.datetime.utcnow, nullable=False, index=True)
    level = Column(String, nullable=False, default="info")
    message = Column(Text, nullable=True)
    data_json = Column("data", JSON, nullable=True, default=dict)

    __table_args__ = (
        Index("ix_job_logs_job_id_id", "job_id", "id"),
    )
