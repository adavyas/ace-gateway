from __future__ import annotations

import re


def user_slug(user_id: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", (user_id or "").strip()).strip("_").lower()
    return slug or "unknown"


def container_name(user_id: str, generation: int) -> str:
    return f"ace-user-{user_slug(user_id)}"


def volume_name(user_id: str) -> str:
    return f"ace_user_{user_slug(user_id)}_data"


def failed_container_name(user_id: str, generation: int, operation_id: str | None = None) -> str:
    suffix = f"failed-g{int(generation)}"
    if operation_id:
        suffix = f"{suffix}-{str(operation_id).replace('-', '')[:8]}"
    return f"{container_name(user_id, generation)}-{suffix}"


def container_labels(user_id: str, generation: int, role: str, image_tag: str) -> dict[str, str]:
    return {
        "ace.managed": "true",
        "ace.user_id": str(user_id),
        "ace.generation": str(int(generation)),
        "ace.role": role,
        "ace.image_tag": image_tag,
        "ace.created_by": "ace-control-plane",
    }


def generation_metadata(
    *,
    user_id: str,
    generation: int,
    role: str,
    image_tag: str,
    env_overrides: dict[str, str] | None = None,
) -> dict[str, object]:
    metadata: dict[str, object] = container_labels(user_id, generation, role, image_tag)
    metadata["env_overrides"] = {str(k): str(v) for k, v in (env_overrides or {}).items()}
    return metadata


def env_overrides_from_generation(row: dict[str, object] | None) -> dict[str, str]:
    if not row:
        return {}
    payload = row.get("docker_labels_json") if isinstance(row, dict) else None
    if not isinstance(payload, dict):
        return {}
    env_overrides = payload.get("env_overrides")
    if not isinstance(env_overrides, dict):
        return {}
    return {str(k): str(v) for k, v in env_overrides.items()}


def runtime_env(
    *,
    user_id: str,
    generation: int,
    role: str,
    env_overrides: dict[str, str] | None = None,
    internal_port: int | None = None,
    data_mount: str | None = None,
    skills_mount: str | None = None,
) -> dict[str, str]:
    env = {
        "ACE_USER_ID": str(user_id),
        "ACE_GENERATION": str(int(generation)),
        "ACE_ROLE": role,
    }
    if internal_port is not None:
        env["PORT"] = str(int(internal_port))
    if data_mount:
        env["HERMES_HOME"] = str(data_mount)
        env["HERMES_DATA_DIR"] = str(data_mount)
    if skills_mount:
        env["HERMES_GLOBAL_SKILLS_DIR"] = str(skills_mount)
    if env_overrides:
        env.update({str(k): str(v) for k, v in env_overrides.items()})
    return env
