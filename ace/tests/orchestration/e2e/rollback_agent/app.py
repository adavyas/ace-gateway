from __future__ import annotations

import os
import time
from threading import Lock

from fastapi import FastAPI
from fastapi.responses import JSONResponse


app = FastAPI(title="ace-rollback-agent")
_started_at = time.monotonic()
_ready_calls = 0
_ready_lock = Lock()


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@app.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "runtime": "rollback-agent",
        "version": os.getenv("ROLLBACK_AGENT_VERSION", "v1"),
    }


@app.get("/readyz")
async def readyz():
    global _ready_calls
    with _ready_lock:
        _ready_calls += 1
        call_no = _ready_calls

    if _env_bool("ACE_FAIL_READY", False):
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "ACE_FAIL_READY"})

    delay = int(os.getenv("ACE_READY_DELAY_SECONDS", "0"))
    if delay > 0 and (time.monotonic() - _started_at) < delay:
        return JSONResponse(status_code=503, content={"status": "not_ready", "reason": "startup_delay"})

    return {
        "ready": True,
        "runtime": "rollback-agent",
        "version": os.getenv("ROLLBACK_AGENT_VERSION", "v1"),
        "ready_calls": call_no,
    }


@app.get("/internal/runtime")
async def runtime():
    return {
        "user_id": os.getenv("ACE_USER_ID"),
        "generation": os.getenv("ACE_GENERATION"),
        "role": os.getenv("ACE_ROLE"),
        "runtime": "rollback-agent",
        "version": os.getenv("ROLLBACK_AGENT_VERSION", "v1"),
        "ready_calls": _ready_calls,
    }
