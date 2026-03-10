"""
hermes_server.py - Standalone FastAPI application meant to run ON THE VPS.

This server acts as the Hermes Agent HTTP gateway.  It is NOT imported by
the main Scope backend; it runs as a completely separate process:

    uvicorn app.routers.hermes_server:app --host 127.0.0.1 --port 7777

The tunnel manager on the Scope backend opens an SSH port-forward to this
server and proxies client requests through it.

Endpoints
---------
POST /chat
    Receives a chat payload and runs the Hermes Agent CLI as a subprocess.
    Streams the agent output back to the caller as Server-Sent Events (SSE).
    SSE event format::

        data: {"type": "token"|"done"|"error", "content": "<str>"}

WS /ws
    WebSocket variant of /chat.  Accepts JSON messages with the same schema
    as the POST body and sends back SSE-style JSON frames over the socket.

GET /health
    Simple liveness probe that returns {"status": "ok"}.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("hermes_server")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Hermes Agent Server",
    description="VPS-side HTTP server that runs the Hermes Agent and streams responses.",
    version="1.0.0",
)

# ---------------------------------------------------------------------------
# Configuration (all overrideable via environment variables)
# ---------------------------------------------------------------------------

HERMES_CLI_CMD: list[str] = json.loads(
    os.getenv("HERMES_CLI_CMD", '["hermes"]')
)
"""
Shell command used to invoke the Hermes Agent CLI.

Override via environment variable HERMES_CLI_CMD (JSON array), e.g.::

    export HERMES_CLI_CMD='["python", "-m", "hermes.cli"]'
"""

AGENT_TIMEOUT: float = float(os.getenv("AGENT_TIMEOUT", "120"))
"""Maximum seconds to wait for the agent to finish a response."""

# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ChatPayload(BaseModel):
    """Request body for POST /chat and WS /ws messages."""

    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    context: Optional[dict[str, Any]] = None
    stream: bool = True


# ---------------------------------------------------------------------------
# Core agent runner
# ---------------------------------------------------------------------------


async def _run_hermes_agent(payload: ChatPayload) -> AsyncIterator[str]:
    """Invoke the Hermes Agent CLI and yield SSE-formatted lines.

    The CLI is called with the user message passed via ``--message`` flag.
    Additional metadata is forwarded as ``--session-id``, ``--user-id``, and
    ``--context`` (JSON-encoded) when present.

    Each yielded string is a complete SSE frame, e.g.::

        "data: {\\"type\\": \\"token\\", \\"content\\": \\"Hello\\"}\n\n"

    The generator emits:

    * ``token`` events for each line of stdout from the agent.
    * A single ``done`` event when the process exits cleanly (code 0).
    * A single ``error`` event when the process exits with a non-zero code or
      raises an exception.

    Yields
    ------
    str
        UTF-8 SSE frames.
    """
    cmd = list(HERMES_CLI_CMD)
    cmd += ["--message", payload.message]
    if payload.session_id:
        cmd += ["--session-id", payload.session_id]
    if payload.user_id:
        cmd += ["--user-id", payload.user_id]
    if payload.context:
        cmd += ["--context", json.dumps(payload.context)]

    logger.info(
        "Starting Hermes agent for user=%s session=%s",
        payload.user_id,
        payload.session_id,
    )

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ},  # inherit environment (activates venv, etc.)
        )
    except FileNotFoundError:
        logger.error("Hermes CLI not found: %s", cmd[0])
        yield _sse("error", f"Hermes CLI not found: {cmd[0]}.  Is 'hermes' on PATH?")
        return
    except Exception as exc:
        logger.exception("Failed to start Hermes agent: %s", exc)
        yield _sse("error", f"Failed to start agent: {exc}")
        return

    # Stream stdout line-by-line as token events
    assert proc.stdout is not None  # guaranteed by PIPE
    try:
        async with asyncio.timeout(AGENT_TIMEOUT):
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    # EOF - process likely finished
                    break
                line = line_bytes.decode("utf-8", errors="replace").rstrip("\n")
                if line:
                    yield _sse("token", line)
    except TimeoutError:
        logger.warning("Agent timed out after %.0fs; killing process.", AGENT_TIMEOUT)
        proc.kill()
        yield _sse("error", "Agent timed out")
        return
    except Exception as exc:
        logger.exception("Error reading agent output: %s", exc)
        proc.kill()
        yield _sse("error", f"Stream read error: {exc}")
        return

    # Wait for process to exit and capture any stderr
    try:
        _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except asyncio.TimeoutError:
        proc.kill()
        stderr_bytes = b""

    exit_code = proc.returncode
    if exit_code == 0:
        yield _sse("done", "")
        logger.info("Hermes agent completed successfully.")
    else:
        stderr_text = (stderr_bytes or b"").decode("utf-8", errors="replace").strip()
        logger.error("Hermes agent exited with code %d: %s", exit_code, stderr_text)
        yield _sse("error", stderr_text or f"Agent exited with code {exit_code}")


def _sse(event_type: str, content: str) -> str:
    """Format a single SSE data frame.

    Returns a string in the form::

        "data: {\\"type\\": \\"token\\", \\"content\\": \\"Hello\\"}\n\n"
    """
    payload = json.dumps({"type": event_type, "content": content})
    return f"data: {payload}\n\n"


# ---------------------------------------------------------------------------
# HTTP Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe.  Returns {"status": "ok"} when the server is reachable."""
    return {"status": "ok"}


@app.post("/chat")
async def chat(payload: ChatPayload) -> Any:
    """Run the Hermes Agent and return the response.

    When ``stream=True`` (default) the response is streamed as SSE.
    When ``stream=False`` all tokens are buffered and returned as JSON::

        {"type": "done", "tokens": ["Hello", " world"]}
    """
    if payload.stream:
        return StreamingResponse(
            _run_hermes_agent(payload),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        # Buffer all tokens into a list
        tokens: list[str] = []
        error_msg: Optional[str] = None
        async for frame in _run_hermes_agent(payload):
            # Each frame is like: "data: {...}\n\n"
            raw = frame.removeprefix("data: ").strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "token":
                tokens.append(event.get("content", ""))
            elif event.get("type") == "error":
                error_msg = event.get("content", "Unknown error")
                break
        if error_msg:
            return {"type": "error", "content": error_msg, "tokens": tokens}
        return {"type": "done", "tokens": tokens, "content": "".join(tokens)}


# ---------------------------------------------------------------------------
# WebSocket Endpoint
# ---------------------------------------------------------------------------


@app.websocket("/ws")
async def ws_chat(websocket: WebSocket) -> None:
    """Bidirectional WebSocket interface to the Hermes Agent.

    The client sends JSON messages matching the ``ChatPayload`` schema.
    The server sends back JSON frames of the form::

        {"type": "token"|"done"|"error", "content": "<str>"}

    The connection stays open so the client may send multiple chat turns.
    """
    await websocket.accept()
    logger.info("WebSocket client connected.")
    try:
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                logger.info("WebSocket client disconnected.")
                break

            try:
                data = json.loads(raw)
                payload = ChatPayload(**data)
            except Exception as exc:
                await websocket.send_text(
                    json.dumps({"type": "error", "content": f"Invalid payload: {exc}"})
                )
                continue

            # Stream agent output back over the WebSocket
            async for frame in _run_hermes_agent(payload):
                # Convert "data: {...}\n\n" SSE format back to raw JSON
                raw_json = frame.removeprefix("data: ").strip()
                if raw_json:
                    try:
                        await websocket.send_text(raw_json)
                    except Exception:
                        break
    except Exception as exc:
        logger.exception("WebSocket handler error: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Entry-point (for direct execution: python hermes_server.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "hermes_server:app",
        host="127.0.0.1",
        port=7777,
        reload=False,
        log_level="info",
    )
