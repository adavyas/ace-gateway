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

# VPS / SSH tunnel settings
VPS_HOST = os.getenv("VPS_HOST", "15.204.88.57")
VPS_USER = os.getenv("VPS_USER", "ubuntu")
VPS_SSH_KEY = os.getenv("VPS_SSH_KEY", "~/.ssh/id_ed25519")
# Back-compat: keep VPS_HERMES_PORT as fallback for existing env files.
VPS_ACE_PORT = int(os.getenv("VPS_ACE_PORT") or os.getenv("VPS_HERMES_PORT", "7777"))
TUNNEL_RECONNECT_INTERVAL = int(os.getenv("TUNNEL_RECONNECT_INTERVAL", "30"))

# User-runtime provisioning on VPS.
ACE_DOCKER_NETWORK = os.getenv("ACE_DOCKER_NETWORK", "ace-net")
ACE_RUNTIME_IMAGE = os.getenv("ACE_RUNTIME_IMAGE", "ace-hermes")
ACE_RUNTIME_IMAGE_TAG = os.getenv("ACE_RUNTIME_IMAGE_TAG", "latest")
ACE_RUNTIME_CONTAINER_PORT = _env_int("ACE_RUNTIME_CONTAINER_PORT", 8080)
ACE_RUNTIME_READINESS_PATH = os.getenv("ACE_RUNTIME_READINESS_PATH", "/readyz")
ACE_RUNTIME_READY_TIMEOUT_SECONDS = _env_float("ACE_RUNTIME_READY_TIMEOUT_SECONDS", 25.0)
ACE_RUNTIME_READY_POLL_SECONDS = _env_float("ACE_RUNTIME_READY_POLL_SECONDS", 0.5)
ACE_RUNTIME_SSH_CONNECT_TIMEOUT = _env_int("ACE_RUNTIME_SSH_CONNECT_TIMEOUT", 5)
ACE_RUNTIME_LOG_TAIL_LINES = _env_int("ACE_RUNTIME_LOG_TAIL_LINES", 200)
ACE_RUNTIME_CACHE_SECONDS = _env_float("ACE_RUNTIME_CACHE_SECONDS", 10.0)
ACE_RUNTIME_LOCK_CONFLICT_RETURNS_409 = _env_flag("ACE_RUNTIME_LOCK_CONFLICT_RETURNS_409", False)
ACE_GLOBAL_SKILLS_DIR = os.getenv("ACE_GLOBAL_SKILLS_DIR", "/opt/ace/skills/current")
ACE_RUNTIME_DATA_MOUNT = os.getenv("ACE_RUNTIME_DATA_MOUNT", "/data")
ACE_RUNTIME_SKILLS_MOUNT = os.getenv("ACE_RUNTIME_SKILLS_MOUNT", "/global_skills")
