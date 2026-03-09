from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ManagerSettings:
    ace_internal_api_token: str = field(default_factory=lambda: os.getenv("ACE_INTERNAL_API_TOKEN", ""))
    ace_docker_network: str = field(default_factory=lambda: os.getenv("ACE_DOCKER_NETWORK", "ace-net"))
    ace_agent_internal_port: int = field(default_factory=lambda: int(os.getenv("ACE_AGENT_INTERNAL_PORT", "8080")))
    ace_agent_data_mount: str = field(default_factory=lambda: os.getenv("ACE_AGENT_DATA_MOUNT", "/data/ace"))
    ace_global_skills_dir: str = field(
        default_factory=lambda: os.getenv("ACE_GLOBAL_SKILLS_DIR", "/opt/ace/skills/current")
    )
    ace_runtime_skills_mount: str = field(
        default_factory=lambda: os.getenv("ACE_RUNTIME_SKILLS_MOUNT", "/global_skills")
    )
    ace_default_image_tag: str = field(default_factory=lambda: os.getenv("ACE_DEFAULT_IMAGE_TAG", "ace-agent:latest"))
    ace_container_startup_grace_seconds: int = field(
        default_factory=lambda: int(os.getenv("ACE_CONTAINER_STARTUP_GRACE_SECONDS", "20"))
    )
    ace_health_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("ACE_HEALTH_TIMEOUT_SECONDS", "90")))
    ace_health_poll_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("ACE_HEALTH_POLL_INTERVAL_SECONDS", "2"))
    )
    ace_drain_timeout_seconds: int = field(default_factory=lambda: int(os.getenv("ACE_DRAIN_TIMEOUT_SECONDS", "20")))
    ace_keep_previous_warm_seconds: int = field(
        default_factory=lambda: int(os.getenv("ACE_KEEP_PREVIOUS_WARM_SECONDS", "300"))
    )
    ace_max_global_concurrent_ops: int = field(
        default_factory=lambda: int(os.getenv("ACE_MAX_GLOBAL_CONCURRENT_OPS", "3"))
    )
    ace_op_max_seconds: int = field(default_factory=lambda: int(os.getenv("ACE_OP_MAX_SECONDS", "600")))
    ace_stale_operation_grace_minutes: int = field(
        default_factory=lambda: int(os.getenv("ACE_STALE_OPERATION_GRACE_MINUTES", "15"))
    )
    ace_health_use_published_port_fallback: bool = field(
        default_factory=lambda: _env_bool("ACE_HEALTH_USE_PUBLISHED_PORT_FALLBACK", True)
    )
    ace_docker_base_url: str = field(default_factory=lambda: os.getenv("ACE_DOCKER_BASE_URL", "").strip())
    ace_bind_host: str = field(default_factory=lambda: os.getenv("ACE_BIND_HOST", "127.0.0.1"))
    ace_bind_port: int = field(default_factory=lambda: int(os.getenv("ACE_BIND_PORT", "8100")))
    ace_enable_gateway_cutover: bool = field(default_factory=lambda: _env_bool("ACE_ENABLE_GATEWAY_CUTOVER", False))
    ace_preserve_failed_containers: bool = field(
        default_factory=lambda: _env_bool("ACE_PRESERVE_FAILED_CONTAINERS", True)
    )


@lru_cache(maxsize=1)
def get_settings() -> ManagerSettings:
    return ManagerSettings()
