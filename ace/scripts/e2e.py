from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg2
import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
MANAGER_URL = os.getenv("ACE_E2E_MANAGER_URL", "http://127.0.0.1:18100")
MANAGER_TOKEN = os.getenv("ACE_E2E_MANAGER_TOKEN", "test-internal-token")
POSTGRES_DSN = os.getenv("ACE_E2E_POSTGRES_DSN", "postgresql://postgres:postgres@127.0.0.1:55432/postgres")
FAKE_AGENT_DIR = Path(os.getenv("ACE_E2E_FAKE_AGENT_DIR", str(REPO_ROOT / "tests/orchestration/e2e/fake_agent")))
FAKE_IMAGE_TAG = os.getenv("ACE_E2E_FAKE_IMAGE_TAG", "ace-agent:e2e")


@dataclass
class E2EUser:
    user_id: str
    email: str


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {MANAGER_TOKEN}",
        "Content-Type": "application/json",
    }


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _log(msg: str) -> None:
    print(f"[e2e] {msg}", flush=True)


def _wait_manager(timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = requests.get(f"{MANAGER_URL}/healthz", timeout=2)
            if r.status_code == 200:
                _log("manager is healthy")
                return
        except Exception:
            pass
        time.sleep(1)
    raise RuntimeError("manager did not become healthy")


def _db_conn():
    return psycopg2.connect(POSTGRES_DSN)


def _new_user_obj(prefix: str) -> E2EUser:
    uid = str(uuid.uuid4())
    return E2EUser(uid, f"{prefix}-{uid[:8]}@test.local")


def _insert_auth_user(user: E2EUser) -> None:
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                insert into auth.users (id, email)
                values (%s, %s)
                on conflict (id) do nothing
                """,
                (user.user_id, user.email),
            )


def _manager_request(method: str, path: str, payload: dict[str, Any] | None = None, expected: int = 200) -> dict[str, Any]:
    url = f"{MANAGER_URL}{path}"
    resp = requests.request(method, url, headers=_headers(), json=payload, timeout=20)
    if resp.status_code != expected:
        raise AssertionError(f"{method} {path} expected {expected} got {resp.status_code}: {resp.text}")
    if not resp.text:
        return {}
    return resp.json()


def _poll_operation(op_id: str, *, timeout: int = 180) -> dict[str, Any]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = _manager_request("GET", f"/operations/{op_id}", expected=200)
        status = data.get("status")
        if status in {"succeeded", "failed", "rolled_back", "cancelled"}:
            return data
        time.sleep(1)
    raise TimeoutError(f"operation {op_id} did not reach terminal state in time")


def _enqueue(path: str, payload: dict[str, Any], *, expected_status: int = 202) -> str:
    data = _manager_request("POST", path, payload=payload, expected=expected_status)
    if expected_status == 202:
        op_id = data.get("operation_id")
        if not op_id:
            raise AssertionError(f"missing operation_id in response: {data}")
        return str(op_id)
    return ""


def _status(user_id: str) -> dict[str, Any]:
    return _manager_request("GET", f"/status/{user_id}", expected=200)


def _docker_ps_for_user(user_id: str) -> str:
    result = _run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=ace.user_id={user_id}",
            "--format",
            "{{.Names}} {{.Status}} {{.Image}}",
        ]
    )
    return (result.stdout or "").strip()


def _cleanup_user_containers(user_id: str) -> None:
    ids = _run(["docker", "ps", "-aq", "--filter", f"label=ace.user_id={user_id}"]).stdout.strip().splitlines()
    if ids:
        _run(["docker", "rm", "-f", *ids], check=False)


def _build_fake_agent_image() -> None:
    _log("building fake agent image")
    _run(["docker", "build", "-t", FAKE_IMAGE_TAG, str(FAKE_AGENT_DIR)])


def _new_user(user: E2EUser) -> dict[str, Any]:
    op_id = _enqueue(
        "/new-user",
        {
            "user_id": user.user_id,
            "image_tag": FAKE_IMAGE_TAG,
            "env_overrides": {},
            "start_immediately": True,
        },
    )
    return _poll_operation(op_id)


def _assert(cond: bool, message: str) -> None:
    if not cond:
        raise AssertionError(message)


def _scenario_happy_path() -> None:
    user = _new_user_obj("happy")
    _insert_auth_user(user)
    try:
        _log("scenario 1: happy path (new-user -> deploy -> rollback -> restart-safe)")
        op = _new_user(user)
        _assert(op["status"] == "succeeded", f"new-user failed: {op}")

        deploy_op = _enqueue(
            f"/deploy/{user.user_id}",
            {"image_tag": FAKE_IMAGE_TAG, "env_overrides": {}, "keep_previous_warm_seconds": 0},
        )
        deploy_result = _poll_operation(deploy_op)
        _assert(deploy_result["status"] == "succeeded", f"deploy failed: {deploy_result}")

        rollback_op = _enqueue(f"/rollback/{user.user_id}", {"reason": "e2e rollback"})
        rollback_result = _poll_operation(rollback_op)
        _assert(rollback_result["status"] == "succeeded", f"rollback failed: {rollback_result}")

        restart_op = _enqueue(f"/restart/{user.user_id}", {"mode": "safe"})
        restart_result = _poll_operation(restart_op)
        _assert(restart_result["status"] == "succeeded", f"restart-safe failed: {restart_result}")
    finally:
        _cleanup_user_containers(user.user_id)


def _scenario_pre_cutover_failure() -> None:
    user = _new_user_obj("pre-fail")
    _insert_auth_user(user)
    try:
        _log("scenario 2: deploy pre-cutover failure")
        _assert(_new_user(user)["status"] == "succeeded", "new-user failed")

        fail_op = _enqueue(
            f"/deploy/{user.user_id}",
            {
                "image_tag": FAKE_IMAGE_TAG,
                "env_overrides": {"ACE_FAIL_READY": "1"},
                "keep_previous_warm_seconds": 0,
            },
        )
        fail_result = _poll_operation(fail_op)
        _assert(fail_result["status"] == "failed", f"expected failed deploy: {fail_result}")
        _assert(
            fail_result["error_code"] in {"readiness_timeout", "active_readiness_timeout", "operation_timeout"},
            f"unexpected code: {fail_result}",
        )
    finally:
        _cleanup_user_containers(user.user_id)


def _scenario_post_cutover_rollback() -> None:
    user = _new_user_obj("post-rb")
    _insert_auth_user(user)
    try:
        _log("scenario 3: deploy succeeds under single-volume rollout model")
        _assert(_new_user(user)["status"] == "succeeded", "new-user failed")

        op = _enqueue(
            f"/deploy/{user.user_id}",
            {
                "image_tag": FAKE_IMAGE_TAG,
                "env_overrides": {"ACE_READY_OK_CALLS": "1"},
                "keep_previous_warm_seconds": 0,
            },
        )
        result = _poll_operation(op)
        _assert(result["status"] == "succeeded", f"expected succeeded: {result}")
    finally:
        _cleanup_user_containers(user.user_id)


def _scenario_lock_conflict() -> None:
    user = _new_user_obj("lock")
    _insert_auth_user(user)
    try:
        _log("scenario 4: lock conflict")
        _assert(_new_user(user)["status"] == "succeeded", "new-user failed")

        op_a = _enqueue(
            f"/deploy/{user.user_id}",
            {
                "image_tag": FAKE_IMAGE_TAG,
                "env_overrides": {"ACE_READY_DELAY_SECONDS": "2"},
                "keep_previous_warm_seconds": 0,
            },
        )
        conflict_resp = requests.post(
            f"{MANAGER_URL}/deploy/{user.user_id}",
            headers=_headers(),
            json={"image_tag": FAKE_IMAGE_TAG, "env_overrides": {}, "keep_previous_warm_seconds": 0},
            timeout=10,
        )
        _assert(conflict_resp.status_code == 409, f"expected 409, got {conflict_resp.status_code}: {conflict_resp.text}")
        _ = _poll_operation(op_a)
    finally:
        _cleanup_user_containers(user.user_id)


def _scenario_timeout_cleanup() -> None:
    user = _new_user_obj("timeout")
    _insert_auth_user(user)
    try:
        _log("scenario 5: deploy readiness failure preserves candidate for inspection")
        _assert(_new_user(user)["status"] == "succeeded", "new-user failed")

        timeout_op = _enqueue(
            f"/deploy/{user.user_id}",
            {
                "image_tag": FAKE_IMAGE_TAG,
                "env_overrides": {"ACE_READY_DELAY_SECONDS": "400"},
                "keep_previous_warm_seconds": 0,
            },
        )
        timeout_result = _poll_operation(timeout_op, timeout=240)
        _assert(timeout_result["status"] == "failed", f"expected timeout failure: {timeout_result}")
        _assert(timeout_result["error_code"] == "readiness_timeout", f"unexpected timeout code: {timeout_result}")

        st = _status(user.user_id)
        _assert(st["runtime_status"] == "active", f"runtime should be reset active: {st}")
        _assert(st["current_generation"] == 1, f"generation should remain 1: {st}")

        containers = _docker_ps_for_user(user.user_id)
        _assert("failed-g2" in containers, f"failed candidate should be preserved for inspection: {containers}")
    finally:
        _cleanup_user_containers(user.user_id)


def main() -> int:
    _log("memory profile: low (single-user scenarios with cleanup between runs)")
    _wait_manager()
    _build_fake_agent_image()

    _scenario_happy_path()
    _scenario_pre_cutover_failure()
    _scenario_post_cutover_rollback()
    _scenario_lock_conflict()
    _scenario_timeout_cleanup()

    _log("all E2E scenarios passed")
    print(json.dumps({"status": "ok", "manager": MANAGER_URL, "memory_profile": "low"}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
