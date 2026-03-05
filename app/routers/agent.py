"""
routers/agent.py - FastAPI router that proxies requests to the ACE runtime
HTTP server running on the VPS via the SSH tunnel managed by TunnelManager.

Endpoints
---------
POST /agent/chat
    Authenticated.  Accepts a ChatRequest and proxies it to the VPS.
    Supports both streaming (SSE) and non-streaming JSON responses.

GET /agent/status
    Unauthenticated.  Returns current tunnel health info.

WebSocket /agent/ws
    Authenticated via ?token= query parameter.
    Bidirectional message proxy to ws://localhost:<ACE_PORT>/ws on the VPS.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

import httpx
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config
from ..auth.deps import CurrentUser, get_current_user
from ..auth.jwt import JWTValidationError, verify_supabase_jwt
from ..runtime_manager import RuntimeOptions, RuntimeProvisioningError, ensure_user_runtime
from ..tunnel import tunnel_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])

# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    """Payload for POST /agent/chat."""

    message: str
    session_id: Optional[str] = None
    stream: bool = True
    context: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _ensure_tunnel() -> None:
    """Ensure the SSH tunnel is running; raise HTTP 503 on failure."""
    try:
        await tunnel_manager.ensure_running()
    except Exception as exc:
        logger.error("Tunnel could not be established: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="ACE runtime tunnel is unavailable. Please retry shortly.",
            headers={"Retry-After": "10"},
        ) from exc


async def _stream_sse(response: httpx.Response) -> AsyncIterator[bytes]:
    """Yield raw SSE bytes from the upstream httpx streaming response."""
    async for chunk in response.aiter_bytes():
        yield chunk


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/chat")
async def agent_chat(
    body: ChatRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> Any:
    """Proxy a chat request to the ACE runtime on the VPS.

    When ``stream=True`` (the default) the endpoint returns a
    ``text/event-stream`` response and forwards SSE chunks as they arrive.

    When ``stream=False`` the full JSON response from the VPS is awaited and
    returned directly.

    Raises
    ------
    503  If the SSH tunnel cannot be established.
    504  If the upstream request times out (> 120 s).
    502  If the upstream returns an unexpected error.
    """
    try:
        await ensure_user_runtime(
            current_user.user_id,
            options=RuntimeOptions(allow_cached=True, wait_for_lock=True),
        )
    except RuntimeProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    await _ensure_tunnel()

    upstream_payload = {
        "message": body.message,
        "session_id": body.session_id,
        "user_id": current_user.user_id,
        "context": body.context or {},
        "stream": body.stream,
    }

    base_url = tunnel_manager.base_url
    target_url = f"{base_url}/chat"
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)

    if body.stream:
        # ----------------------------------------------------------------
        # Streaming path: forward SSE chunks back to the client.
        # ----------------------------------------------------------------
        async def _event_generator() -> AsyncIterator[bytes]:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        "POST",
                        target_url,
                        json=upstream_payload,
                        headers={"Accept": "text/event-stream"},
                    ) as upstream_resp:
                        if upstream_resp.status_code >= 500:
                            error_body = await upstream_resp.aread()
                            logger.error(
                                "Upstream /chat returned %d: %s",
                                upstream_resp.status_code,
                                error_body,
                            )
                            yield _sse_event(
                                "error",
                                f"Upstream error {upstream_resp.status_code}",
                            )
                            return
                        async for chunk in upstream_resp.aiter_bytes():
                            if chunk:
                                yield chunk
            except httpx.TimeoutException as exc:
                logger.warning("Upstream /chat timed out: %s", exc)
                yield _sse_event("error", "Request timed out")
            except httpx.ConnectError as exc:
                logger.error("Cannot connect through tunnel: %s", exc)
                yield _sse_event("error", "Tunnel connection failed")
            except Exception as exc:
                logger.exception("Unexpected streaming error: %s", exc)
                yield _sse_event("error", "Internal proxy error")

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering
            },
        )

    else:
        # ----------------------------------------------------------------
        # Non-streaming path: wait for complete JSON.
        # ----------------------------------------------------------------
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                upstream_resp = await client.post(target_url, json=upstream_payload)
        except httpx.TimeoutException as exc:
            raise HTTPException(status_code=504, detail="Upstream request timed out") from exc
        except httpx.ConnectError as exc:
            raise HTTPException(
                status_code=503,
                detail="Tunnel connection failed",
                headers={"Retry-After": "5"},
            ) from exc
        except Exception as exc:
            logger.exception("Proxy error: %s", exc)
            raise HTTPException(status_code=502, detail="Upstream proxy error") from exc

        if upstream_resp.status_code == 200:
            return upstream_resp.json()
        raise HTTPException(
            status_code=upstream_resp.status_code,
            detail=f"Upstream returned {upstream_resp.status_code}",
        )


@router.get("/status")
async def agent_status() -> dict[str, Any]:
    """Return the current SSH tunnel health status.

    This endpoint is publicly accessible (no auth required) so dashboards
    and health-check systems can query it without a bearer token.
    """
    active = tunnel_manager.is_active
    port: Optional[int] = None
    if active:
        try:
            port = tunnel_manager._local_port
        except Exception:
            pass
    return {
        "tunnel_active": active,
        "tunnel_port": port,
        "vps_host": config.VPS_HOST,
    }


@router.websocket("/ws")
async def agent_ws(
    websocket: WebSocket,
    token: str = Query(..., description="Supabase JWT bearer token"),
) -> None:
    """Bidirectional WebSocket proxy to the ACE runtime on the VPS.

    Authentication is performed via the ``?token=<jwt>`` query parameter
    because the WebSocket handshake cannot carry custom HTTP headers in most
    browser environments.

    Messages are forwarded transparently in both directions until either the
    client or the VPS closes the connection.
    """
    # -- Auth ----------------------------------------------------------------
    try:
        claims = verify_supabase_jwt(token)
    except JWTValidationError as exc:
        await websocket.close(code=4001, reason=str(exc))
        return

    try:
        await ensure_user_runtime(
            str(claims.get("sub") or ""),
            options=RuntimeOptions(allow_cached=True, wait_for_lock=True),
        )
    except RuntimeProvisioningError:
        await websocket.close(code=4503, reason="Runtime unavailable")
        return

    # -- Tunnel --------------------------------------------------------------
    try:
        await tunnel_manager.ensure_running()
    except Exception:
        await websocket.close(code=4503, reason="Tunnel unavailable")
        return

    vps_ws_url = f"ws://127.0.0.1:{tunnel_manager._local_port}/ws"

    await websocket.accept()

    # Connect to VPS WebSocket
    try:
        async with httpx.AsyncClient() as _client:
            # httpx does not support WebSocket; use the lower-level approach
            # via asyncio streams through the tunnel's TCP port.
            pass
    except Exception:
        pass

    # Use a pure asyncio TCP approach so we stay dependency-free.
    # We implement a minimal WebSocket client over raw asyncio streams.
    # For production, install `websockets` or `wsproto`; here we use
    # the websockets library if available, otherwise emit a clear error.
    try:
        import websockets  # type: ignore[import]
    except ImportError:
        await websocket.send_text(
            json.dumps({
                "type": "error",
                "content": (
                    "Server-side websockets package is not installed. "
                    "Please run: pip install websockets"
                ),
            })
        )
        await websocket.close(code=1011)
        return

    async def _forward_client_to_vps(
        ws_client: WebSocket,
        ws_vps: Any,
    ) -> None:
        """Read from the FastAPI client and send to the VPS WebSocket."""
        try:
            while True:
                data = await ws_client.receive_text()
                await ws_vps.send(data)
        except (WebSocketDisconnect, Exception):
            pass

    async def _forward_vps_to_client(
        ws_vps: Any,
        ws_client: WebSocket,
    ) -> None:
        """Read from the VPS WebSocket and send to the FastAPI client."""
        try:
            async for message in ws_vps:
                if isinstance(message, bytes):
                    await ws_client.send_bytes(message)
                else:
                    await ws_client.send_text(message)
        except Exception:
            pass

    try:
        async with websockets.connect(vps_ws_url) as vps_ws:
            fwd_up = asyncio.create_task(_forward_client_to_vps(websocket, vps_ws))
            fwd_down = asyncio.create_task(_forward_vps_to_client(vps_ws, websocket))
            done, pending = await asyncio.wait(
                {fwd_up, fwd_down},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
    except Exception as exc:
        logger.error("WebSocket proxy error: %s", exc)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _sse_event(event_type: str, content: str) -> bytes:
    """Format a single SSE frame with a JSON data payload.

    Output format::

        data: {"type": "error", "content": "..."}\\n\\n
    """
    payload = json.dumps({"type": event_type, "content": content})
    return f"data: {payload}\n\n".encode()
