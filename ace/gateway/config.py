import os
from dotenv import load_dotenv


load_dotenv()


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value.strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value.strip())
    except Exception:
        return default


SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
# Supabase now prefers publishable/secret keys.
# Keep anon/service_role fallback for older projects.
SUPABASE_PUBLISHABLE_KEY = os.getenv("SUPABASE_PUBLISHABLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SECRET_KEY = os.getenv("SUPABASE_SECRET_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")

SUPABASE_AUTH_ISSUER = f"{SUPABASE_URL}/auth/v1" if SUPABASE_URL else ""
SUPABASE_JWKS_URL = f"{SUPABASE_AUTH_ISSUER}/.well-known/jwks.json" if SUPABASE_AUTH_ISSUER else ""
SUPABASE_JWT_AUDIENCE = os.getenv("SUPABASE_JWT_AUDIENCE", "authenticated")
SUPABASE_VERIFY_AUDIENCE = _env_flag("SUPABASE_VERIFY_AUDIENCE", True)
SUPABASE_VERIFY_ISSUER = _env_flag("SUPABASE_VERIFY_ISSUER", True)
SUPABASE_JWKS_CACHE_SECONDS = int(os.getenv("SUPABASE_JWKS_CACHE_SECONDS", "300"))

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))

# Local upstream settings (same VPS/host deployment)
ACE_UPSTREAM_SCHEME = os.getenv("ACE_UPSTREAM_SCHEME", "http")
ACE_UPSTREAM_HOST = os.getenv("ACE_UPSTREAM_HOST", "127.0.0.1")
# Back-compat: keep VPS_HERMES_PORT/VPS_ACE_PORT fallback for existing env files.
ACE_UPSTREAM_PORT = int(
    os.getenv("ACE_UPSTREAM_PORT")
    or os.getenv("VPS_ACE_PORT")
    or os.getenv("VPS_HERMES_PORT", "7777")
)
ACE_UPSTREAM_BASE_PATH = os.getenv("ACE_UPSTREAM_BASE_PATH", "")
ACE_UPSTREAM_BASE_URL = (
    f"{ACE_UPSTREAM_SCHEME}://{ACE_UPSTREAM_HOST}:{ACE_UPSTREAM_PORT}"
    f"{ACE_UPSTREAM_BASE_PATH}".rstrip("/")
)

# User-runtime provisioning on same host.
ACE_DOCKER_NETWORK = os.getenv("ACE_DOCKER_NETWORK", "ace-net")
ACE_RUNTIME_IMAGE = os.getenv("ACE_RUNTIME_IMAGE", "ace-hermes")
ACE_RUNTIME_IMAGE_TAG = os.getenv("ACE_RUNTIME_IMAGE_TAG", "latest")
ACE_RUNTIME_CONTAINER_PORT = _env_int("ACE_RUNTIME_CONTAINER_PORT", 8080)
ACE_RUNTIME_READINESS_PATH = os.getenv("ACE_RUNTIME_READINESS_PATH", "/readyz")
ACE_RUNTIME_READY_TIMEOUT_SECONDS = _env_float("ACE_RUNTIME_READY_TIMEOUT_SECONDS", 25.0)
ACE_RUNTIME_READY_POLL_SECONDS = _env_float("ACE_RUNTIME_READY_POLL_SECONDS", 0.5)
ACE_RUNTIME_LOG_TAIL_LINES = _env_int("ACE_RUNTIME_LOG_TAIL_LINES", 200)
ACE_RUNTIME_CACHE_SECONDS = _env_float("ACE_RUNTIME_CACHE_SECONDS", 10.0)
ACE_RUNTIME_LOCK_CONFLICT_RETURNS_409 = _env_flag("ACE_RUNTIME_LOCK_CONFLICT_RETURNS_409", False)
ACE_GLOBAL_SKILLS_DIR = os.getenv("ACE_GLOBAL_SKILLS_DIR", "/opt/ace/skills/current")
ACE_RUNTIME_DATA_MOUNT = os.getenv("ACE_RUNTIME_DATA_MOUNT", "/data/ace")
ACE_RUNTIME_SKILLS_MOUNT = os.getenv("ACE_RUNTIME_SKILLS_MOUNT", "/global_skills")

# Messaging ingress
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
ACE_INTERNAL_API_TOKEN = os.getenv("ACE_INTERNAL_API_TOKEN", "").strip()
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
TWILIO_REPLY_CHAR_LIMIT = _env_int("TWILIO_REPLY_CHAR_LIMIT", 1200)
LINQ_WEBHOOK_TOKEN = os.getenv("LINQ_WEBHOOK_TOKEN", "").strip()
LINQ_REPLY_URL = os.getenv("LINQ_REPLY_URL", "").rstrip("/")
LINQ_API_KEY = os.getenv("LINQ_API_KEY", "").strip()
LINQ_API_KEY_HEADER = os.getenv("LINQ_API_KEY_HEADER", "Authorization").strip() or "Authorization"
LINQ_REPLY_TIMEOUT_SECONDS = _env_float("LINQ_REPLY_TIMEOUT_SECONDS", 15.0)
