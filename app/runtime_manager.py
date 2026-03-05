from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import config

logger = logging.getLogger(__name__)

_SAFE_RE = re.compile(r"[^a-zA-Z0-9_-]")
_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_MEMORY_RE = re.compile(r"^[0-9]+(?:[bkmgBKMG])?$")

_REQUIRED_ENV_KEYS = {
    "ACE_USER_ID",
    "PORT",
    "HERMES_HOME",
    "HERMES_GLOBAL_SKILLS_DIR",
}

_user_locks_guard = asyncio.Lock()
_user_locks: dict[str, asyncio.Lock] = {}
_runtime_ready_cache: dict[str, float] = {}


@dataclass(frozen=True)
class RuntimeIdentity:
    user_id: str
    safe_user_id: str
    container_name: str
    volume_name: str
    upstream_url: str


@dataclass(frozen=True)
class RuntimeOptions:
    image_tag: Optional[str] = None
    cpus: Optional[float] = None
    memory: Optional[str] = None
    env_overrides: Optional[dict[str, str]] = None
    allow_cached: bool = False
    wait_for_lock: bool = True


@dataclass(frozen=True)
class RuntimeEnsureResult:
    user_id: str
    container_name: str
    volume_name: str
    upstream_url: str
    status: str
    image_ref: str
    cached: bool = False


class RuntimeProvisioningError(Exception):
    def __init__(self, detail: str, *, status_code: int = 500) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _sanitize_user_fragment(user_id: str) -> str:
    raw = (user_id or "").strip()
    if not raw:
        raise RuntimeProvisioningError("Missing user id", status_code=400)
    safe = _SAFE_RE.sub("_", raw).strip("_")
    if not safe:
        raise RuntimeProvisioningError("User id has no safe characters", status_code=400)
    return safe.lower()


def _runtime_identity(user_id: str) -> RuntimeIdentity:
    safe_user_id = _sanitize_user_fragment(user_id)
    container_name = f"ace-user-{safe_user_id}"
    volume_name = f"ace_user_{safe_user_id}_data"
    upstream_url = f"http://{container_name}:{config.ACE_RUNTIME_CONTAINER_PORT}"
    return RuntimeIdentity(
        user_id=user_id,
        safe_user_id=safe_user_id,
        container_name=container_name,
        volume_name=volume_name,
        upstream_url=upstream_url,
    )


def _image_ref(image_tag: Optional[str]) -> str:
    tag = (image_tag or config.ACE_RUNTIME_IMAGE_TAG or "latest").strip()
    image = (config.ACE_RUNTIME_IMAGE or "ace-hermes").strip()
    if ":" in image:
        return image
    return f"{image}:{tag}"


def _validate_env_overrides(env_overrides: Optional[dict[str, str]]) -> dict[str, str]:
    if not env_overrides:
        return {}
    cleaned: dict[str, str] = {}
    for key, value in env_overrides.items():
        k = str(key or "").strip().upper()
        if not _ENV_KEY_RE.match(k):
            raise RuntimeProvisioningError(
                f"Invalid env key '{key}' in env_overrides",
                status_code=400,
            )
        if k in _REQUIRED_ENV_KEYS:
            raise RuntimeProvisioningError(
                f"env_overrides may not override '{k}'",
                status_code=400,
            )
        v = str(value or "")
        if len(v) > 2048:
            raise RuntimeProvisioningError(
                f"Env value too long for key '{k}'",
                status_code=400,
            )
        cleaned[k] = v
    return cleaned


def _validate_memory(memory: Optional[str]) -> Optional[str]:
    if memory is None:
        return None
    m = memory.strip()
    if not m:
        return None
    if not _MEMORY_RE.match(m):
        raise RuntimeProvisioningError(
            "resources.memory must look like 512m, 2g, or bytes",
            status_code=400,
        )
    return m


async def _get_user_lock(safe_user_id: str) -> asyncio.Lock:
    async with _user_locks_guard:
        lock = _user_locks.get(safe_user_id)
        if lock is None:
            lock = asyncio.Lock()
            _user_locks[safe_user_id] = lock
        return lock


def _is_cached(safe_user_id: str) -> bool:
    ttl = max(float(config.ACE_RUNTIME_CACHE_SECONDS or 0.0), 0.0)
    if ttl <= 0:
        return False
    last = _runtime_ready_cache.get(safe_user_id)
    if last is None:
        return False
    return (time.monotonic() - last) < ttl


def _mark_cached(safe_user_id: str) -> None:
    _runtime_ready_cache[safe_user_id] = time.monotonic()


async def _run_ssh(remote_cmd: str, *, timeout_seconds: float, check: bool = True) -> tuple[int, str, str]:
    ssh_key_path = os.path.expanduser(config.VPS_SSH_KEY)
    target = f"{config.VPS_USER}@{config.VPS_HOST}"
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"ConnectTimeout={int(config.ACE_RUNTIME_SSH_CONNECT_TIMEOUT)}",
        "-i",
        ssh_key_path,
        target,
        remote_cmd,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        proc.kill()
        raise RuntimeProvisioningError(
            f"SSH command timed out after {timeout_seconds:.1f}s",
            status_code=500,
        ) from exc

    stdout = stdout_b.decode("utf-8", errors="replace").strip()
    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    rc = int(proc.returncode or 0)
    if check and rc != 0:
        msg = stderr or stdout or f"remote command failed with exit code {rc}"
        raise RuntimeProvisioningError(msg, status_code=500)
    return rc, stdout, stderr


def _docker_cmd(*parts: str) -> str:
    return shlex.join([str(p) for p in parts if p is not None])


async def _ensure_network_exists() -> None:
    network = config.ACE_DOCKER_NETWORK
    inspect_cmd = _docker_cmd("docker", "network", "inspect", network)
    rc, _, _ = await _run_ssh(inspect_cmd, timeout_seconds=10.0, check=False)
    if rc == 0:
        return
    create_cmd = _docker_cmd("docker", "network", "create", network)
    await _run_ssh(create_cmd, timeout_seconds=15.0, check=True)


async def _ensure_volume_exists(identity: RuntimeIdentity) -> None:
    inspect_cmd = _docker_cmd("docker", "volume", "inspect", identity.volume_name)
    rc, _, _ = await _run_ssh(inspect_cmd, timeout_seconds=10.0, check=False)
    if rc == 0:
        return
    create_cmd = _docker_cmd(
        "docker",
        "volume",
        "create",
        "--name",
        identity.volume_name,
        "--label",
        f"ace.user_id={identity.user_id}",
        "--label",
        "ace.purpose=data",
        "--label",
        "ace.managed_by=gateway",
    )
    await _run_ssh(create_cmd, timeout_seconds=15.0, check=True)


async def _container_status(container_name: str) -> Optional[str]:
    cmd = f"docker container inspect -f '{{{{.State.Status}}}}' {shlex.quote(container_name)}"
    rc, stdout, _ = await _run_ssh(cmd, timeout_seconds=10.0, check=False)
    if rc != 0:
        return None
    status = (stdout or "").strip().lower()
    return status or None


async def _start_container(container_name: str) -> None:
    cmd = _docker_cmd("docker", "start", container_name)
    await _run_ssh(cmd, timeout_seconds=20.0, check=True)


async def _ensure_connected_to_network(container_name: str) -> None:
    cmd = _docker_cmd("docker", "network", "connect", config.ACE_DOCKER_NETWORK, container_name)
    rc, stdout, stderr = await _run_ssh(cmd, timeout_seconds=10.0, check=False)
    if rc == 0:
        return
    joined = f"{stdout}\n{stderr}".lower()
    if "already exists" in joined or "already connected" in joined:
        return
    raise RuntimeProvisioningError(
        f"Failed to connect container to network {config.ACE_DOCKER_NETWORK}: {stderr or stdout}",
        status_code=500,
    )


async def _create_container(identity: RuntimeIdentity, *, options: RuntimeOptions, image_ref: str) -> None:
    env_vars = {
        "ACE_USER_ID": identity.user_id,
        "PORT": str(config.ACE_RUNTIME_CONTAINER_PORT),
        "HERMES_HOME": config.ACE_RUNTIME_DATA_MOUNT,
        "HERMES_GLOBAL_SKILLS_DIR": config.ACE_RUNTIME_SKILLS_MOUNT,
    }
    env_vars.update(_validate_env_overrides(options.env_overrides))

    run_args: list[str] = [
        "docker",
        "run",
        "-d",
        "--name",
        identity.container_name,
        "--network",
        config.ACE_DOCKER_NETWORK,
        "--restart",
        "unless-stopped",
        "--label",
        f"ace.user_id={identity.user_id}",
        "--label",
        "ace.role=user-agent",
        "--label",
        "ace.managed_by=gateway",
    ]

    if options.cpus is not None:
        cpus = float(options.cpus)
        if cpus <= 0:
            raise RuntimeProvisioningError("resources.cpus must be > 0", status_code=400)
        run_args.extend(["--cpus", str(cpus)])

    memory = _validate_memory(options.memory)
    if memory:
        run_args.extend(["--memory", memory])

    for key, value in env_vars.items():
        run_args.extend(["-e", f"{key}={value}"])

    run_args.extend(
        [
            "-v",
            f"{identity.volume_name}:{config.ACE_RUNTIME_DATA_MOUNT}",
            "-v",
            f"{config.ACE_GLOBAL_SKILLS_DIR}:{config.ACE_RUNTIME_SKILLS_MOUNT}:ro",
            image_ref,
        ]
    )
    cmd = _docker_cmd(*run_args)
    await _run_ssh(cmd, timeout_seconds=45.0, check=True)


async def _probe_http_ready(upstream_url: str) -> bool:
    path = config.ACE_RUNTIME_READINESS_PATH
    if not path.startswith("/"):
        path = "/" + path
    ready_url = f"{upstream_url.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(ready_url)
            return resp.status_code < 400
    except Exception:
        return False


async def _probe_inside_container(container_name: str) -> tuple[bool, str]:
    path = config.ACE_RUNTIME_READINESS_PATH
    if not path.startswith("/"):
        path = "/" + path
    probe = (
        "if command -v curl >/dev/null 2>&1; then "
        f"curl -fsS http://127.0.0.1:{config.ACE_RUNTIME_CONTAINER_PORT}{path} >/dev/null; "
        "elif command -v wget >/dev/null 2>&1; then "
        f"wget -q -O - http://127.0.0.1:{config.ACE_RUNTIME_CONTAINER_PORT}{path} >/dev/null; "
        "else exit 127; fi"
    )
    cmd = (
        f"docker exec {shlex.quote(container_name)} sh -lc "
        f"{shlex.quote(probe)}"
    )
    rc, stdout, stderr = await _run_ssh(cmd, timeout_seconds=6.0, check=False)
    if rc == 0:
        return True, ""
    return False, (stderr or stdout or f"probe exit code {rc}")


async def _container_logs_tail(container_name: str, *, lines: int) -> str:
    cmd = f"docker logs --tail {int(lines)} {shlex.quote(container_name)} 2>&1 || true"
    _, stdout, stderr = await _run_ssh(cmd, timeout_seconds=10.0, check=False)
    return (stdout or stderr or "").strip()


async def _wait_ready(identity: RuntimeIdentity) -> None:
    timeout = max(float(config.ACE_RUNTIME_READY_TIMEOUT_SECONDS), 1.0)
    poll = max(float(config.ACE_RUNTIME_READY_POLL_SECONDS), 0.1)
    deadline = time.monotonic() + timeout
    last_probe_error = ""

    while time.monotonic() < deadline:
        if await _probe_http_ready(identity.upstream_url):
            return

        ok, err = await _probe_inside_container(identity.container_name)
        if ok:
            return
        if err:
            last_probe_error = err
        await asyncio.sleep(poll)

    logs_tail = await _container_logs_tail(
        identity.container_name,
        lines=max(int(config.ACE_RUNTIME_LOG_TAIL_LINES), 50),
    )
    logger.error(
        "Container readiness timeout user_id=%s container=%s probe_error=%s logs_tail=%s",
        identity.user_id,
        identity.container_name,
        last_probe_error,
        logs_tail,
    )
    raise RuntimeProvisioningError(
        "Container is not ready yet; retry shortly",
        status_code=503,
    )


async def ensure_user_runtime(
    user_id: str,
    *,
    options: Optional[RuntimeOptions] = None,
) -> RuntimeEnsureResult:
    opts = options or RuntimeOptions()
    identity = _runtime_identity(user_id)
    image_ref = _image_ref(opts.image_tag)

    if opts.allow_cached and _is_cached(identity.safe_user_id):
        return RuntimeEnsureResult(
            user_id=identity.user_id,
            container_name=identity.container_name,
            volume_name=identity.volume_name,
            upstream_url=identity.upstream_url,
            status="running",
            image_ref=image_ref,
            cached=True,
        )

    lock = await _get_user_lock(identity.safe_user_id)
    if lock.locked() and not opts.wait_for_lock and config.ACE_RUNTIME_LOCK_CONFLICT_RETURNS_409:
        raise RuntimeProvisioningError(
            "Provisioning already in progress for this user",
            status_code=409,
        )

    async with lock:
        if opts.allow_cached and _is_cached(identity.safe_user_id):
            return RuntimeEnsureResult(
                user_id=identity.user_id,
                container_name=identity.container_name,
                volume_name=identity.volume_name,
                upstream_url=identity.upstream_url,
                status="running",
                image_ref=image_ref,
                cached=True,
            )

        await _ensure_network_exists()
        await _ensure_volume_exists(identity)

        status = await _container_status(identity.container_name)
        if status is None:
            await _create_container(identity, options=opts, image_ref=image_ref)
        else:
            if status != "running":
                await _start_container(identity.container_name)
            await _ensure_connected_to_network(identity.container_name)

        await _wait_ready(identity)
        _mark_cached(identity.safe_user_id)
        return RuntimeEnsureResult(
            user_id=identity.user_id,
            container_name=identity.container_name,
            volume_name=identity.volume_name,
            upstream_url=identity.upstream_url,
            status="running",
            image_ref=image_ref,
            cached=False,
        )
