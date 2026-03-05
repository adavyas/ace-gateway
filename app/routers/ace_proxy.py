from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from ..auth.deps import CurrentUser, get_current_user
from ..tunnel import tunnel_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ace", tags=["ace"])

_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def _filter_request_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }


def _filter_response_headers(headers: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS
    }


async def _ensure_tunnel() -> None:
    try:
        await tunnel_manager.ensure_running()
    except Exception as exc:
        logger.error("ACE tunnel unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="ACE upstream is unavailable. Please retry shortly.",
            headers={"Retry-After": "10"},
        ) from exc


def _sse_error(content: str) -> bytes:
    payload = json.dumps({"type": "error", "content": content})
    return f"data: {payload}\n\n".encode("utf-8")


@router.get("/health")
async def ace_health() -> dict[str, Any]:
    await _ensure_tunnel()
    target_url = f"{tunnel_manager.base_url}/"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            upstream = await client.get(target_url)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"ACE health probe failed: {exc}") from exc
    return {
        "status": "ok" if upstream.status_code < 500 else "degraded",
        "upstream_status": upstream.status_code,
    }


@router.api_route(
    "/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
)
async def proxy_ace_path(
    path: str,
    request: Request,
    current_user: CurrentUser = Depends(get_current_user),
) -> Response:
    await _ensure_tunnel()

    target_url = f"{tunnel_manager.base_url}/{path.lstrip('/')}"
    params = list(request.query_params.multi_items())
    body = await request.body()

    request_headers = _filter_request_headers(request.headers)
    request_headers["x-ace-user-id"] = current_user.user_id
    if current_user.email:
        request_headers["x-ace-user-email"] = current_user.email

    wants_sse = "text/event-stream" in request.headers.get("accept", "").lower()
    timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

    if wants_sse:
        async def _stream() -> AsyncIterator[bytes]:
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream(
                        request.method,
                        target_url,
                        params=params,
                        headers=request_headers,
                        content=body,
                    ) as upstream:
                        if upstream.status_code >= 400:
                            error_body = await upstream.aread()
                            logger.error(
                                "ACE upstream stream returned %s for %s: %s",
                                upstream.status_code,
                                target_url,
                                error_body.decode("utf-8", errors="replace"),
                            )
                            yield _sse_error(
                                f"ACE upstream returned HTTP {upstream.status_code}"
                            )
                            return
                        async for chunk in upstream.aiter_bytes():
                            if chunk:
                                yield chunk
            except httpx.TimeoutException:
                yield _sse_error("ACE upstream timed out")
            except Exception as exc:
                logger.exception("ACE SSE proxy failed for %s: %s", target_url, exc)
                yield _sse_error("ACE upstream proxy failure")

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.request(
                request.method,
                target_url,
                params=params,
                headers=request_headers,
                content=body,
            )
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="ACE upstream timed out") from exc
    except Exception as exc:
        logger.exception("ACE proxy failed for %s: %s", target_url, exc)
        raise HTTPException(status_code=502, detail="ACE upstream proxy failure") from exc

    if upstream.headers.get("content-type", "").startswith("application/json"):
        try:
            return JSONResponse(
                status_code=upstream.status_code,
                content=upstream.json(),
                headers=_filter_response_headers(upstream.headers),
            )
        except Exception:
            pass

    return Response(
        status_code=upstream.status_code,
        content=upstream.content,
        headers=_filter_response_headers(upstream.headers),
        media_type=upstream.headers.get("content-type"),
    )
