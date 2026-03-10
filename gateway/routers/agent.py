from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config
from ..auth.deps import CurrentUser, get_current_user
from ..auth.jwt import JWTValidationError, verify_supabase_jwt
from ..runtime_manager import RuntimeOptions, RuntimeProvisioningError, ensure_user_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/agent", tags=["agent"])


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: bool = True
    context: Optional[dict[str, Any]] = None


def _to_ws_url(http_url: str) -> str:
    parsed = urlsplit(http_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/ws"


@router.post("/chat")
async def agent_chat(
    body: ChatRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> Any:
    try:
        runtime = await ensure_user_runtime(
            current_user.user_id,
            options=RuntimeOptions(allow_cached=True, wait_for_lock=True),
        )
    except RuntimeProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    upstream_payload = {
        "message": body.message,
        "session_id": body.session_id,
        "user_id": current_user.user_id,
        "context": body.context or {},
        "stream": body.stream,
    }
    target_url = f"{runtime.upstream_url}/chat"
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)

    if body.stream:
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
                            yield _sse_event("error", f"Upstream error {upstream_resp.status_code}")
                            return
                        async for chunk in upstream_resp.aiter_bytes():
                            if chunk:
                                yield chunk
            except httpx.TimeoutException:
                yield _sse_event("error", "Request timed out")
            except httpx.ConnectError:
                yield _sse_event("error", "Runtime connection failed")
            except Exception:
                logger.exception("Unexpected streaming error")
                yield _sse_event("error", "Internal proxy error")

        return StreamingResponse(
            _event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream_resp = await client.post(target_url, json=upstream_payload)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Upstream request timed out") from exc
    except httpx.ConnectError as exc:
        raise HTTPException(
            status_code=503,
            detail="Runtime connection failed",
            headers={"Retry-After": "5"},
        ) from exc
    except Exception as exc:
        logger.exception("Proxy error")
        raise HTTPException(status_code=502, detail="Upstream proxy error") from exc

    if upstream_resp.status_code == 200:
        return upstream_resp.json()
    raise HTTPException(
        status_code=upstream_resp.status_code,
        detail=f"Upstream returned {upstream_resp.status_code}",
    )


@router.get("/status")
async def agent_status() -> dict[str, Any]:
    return {
        "mode": "local",
        "upstream_base_url": config.ACE_UPSTREAM_BASE_URL,
        "runtime_network": config.ACE_DOCKER_NETWORK,
    }


@router.websocket("/ws")
async def agent_ws(
    websocket: WebSocket,
    token: str = Query(..., description="Supabase JWT bearer token"),
) -> None:
    try:
        claims = verify_supabase_jwt(token)
    except JWTValidationError as exc:
        await websocket.close(code=4001, reason=str(exc))
        return

    user_id = str(claims.get("sub") or "")
    try:
        runtime = await ensure_user_runtime(
            user_id,
            options=RuntimeOptions(allow_cached=True, wait_for_lock=True),
        )
    except RuntimeProvisioningError:
        await websocket.close(code=4503, reason="Runtime unavailable")
        return

    upstream_ws_url = _to_ws_url(runtime.upstream_url)
    await websocket.accept()

    try:
        import websockets  # type: ignore[import]
    except ImportError:
        await websocket.send_text(
            json.dumps(
                {
                    "type": "error",
                    "content": (
                        "Server-side websockets package is not installed. "
                        "Please run: pip install websockets"
                    ),
                }
            )
        )
        await websocket.close(code=1011)
        return

    async def _forward_client_to_vps(ws_client: WebSocket, ws_vps: Any) -> None:
        try:
            while True:
                data = await ws_client.receive_text()
                await ws_vps.send(data)
        except (WebSocketDisconnect, Exception):
            pass

    async def _forward_vps_to_client(ws_vps: Any, ws_client: WebSocket) -> None:
        try:
            async for message in ws_vps:
                if isinstance(message, bytes):
                    await ws_client.send_bytes(message)
                else:
                    await ws_client.send_text(message)
        except Exception:
            pass

    try:
        async with websockets.connect(upstream_ws_url) as vps_ws:
            fwd_up = asyncio.create_task(_forward_client_to_vps(websocket, vps_ws))
            fwd_down = asyncio.create_task(_forward_vps_to_client(vps_ws, websocket))
            _, pending = await asyncio.wait(
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


def _sse_event(event_type: str, content: str) -> bytes:
    payload = json.dumps({"type": event_type, "content": content})
    return f"data: {payload}\n\n".encode()
