from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from datetime import datetime
from typing import Any, Optional
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from .. import config
from ..runtime_manager import RuntimeOptions, RuntimeProvisioningError, ensure_user_runtime

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/messaging", tags=["messaging"])

_RELINK_KEYWORDS = {"link", "login", "oauth", "relink", "reset", "sign in", "signin", "start"}
_TWILIO_PROVIDER = "twilio_whatsapp"
_LINQ_PROVIDER = "linq_imessage"
_TWILIO_CHANNEL = "whatsapp"
_LINQ_CHANNEL = "imessage"


def _db_bundle():
    try:
        from db import models as models_module
        from db.database import SessionLocal as session_local, engine as db_engine
        from db.models import Base as db_base
    except ModuleNotFoundError:
        from ace.db import models as models_module
        from ace.db.database import SessionLocal as session_local, engine as db_engine
        from ace.db.models import Base as db_base
    return models_module, session_local, db_engine, db_base


def ensure_messaging_tables() -> None:
    models_module, _, db_engine, db_base = _db_bundle()
    db_base.metadata.create_all(bind=db_engine, tables=[models_module.MessagingLink.__table__])


def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _normalize_address(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("whatsapp:"):
        raw = raw.split(":", 1)[1]
    digits = "".join(ch for ch in raw if ch.isdigit())
    if digits:
        if raw.startswith("+"):
            return f"+{digits}"
        return digits
    return raw.lower()


def _twiml_message(message: str) -> Response:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<Response><Message>{escape(message)}</Message></Response>"
    )
    return Response(content=xml, media_type="application/xml")


def _clip_reply(text: str, limit: int) -> str:
    cleaned = " ".join((text or "").split()).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(limit - 3, 1)].rstrip() + "..."


def _candidate_request_urls(request: Request) -> list[str]:
    candidates: list[str] = []

    def _append(base: str) -> None:
        base = (base or "").rstrip("/")
        if not base:
            return
        url = f"{base}{request.url.path}"
        if request.url.query:
            url = f"{url}?{request.url.query}"
        if url not in candidates:
            candidates.append(url)

    if config.PUBLIC_BASE_URL:
        _append(config.PUBLIC_BASE_URL)

    host = request.headers.get("host", "").strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    forwarded_proto = request.headers.get("x-forwarded-proto", "").strip() or "https"

    if forwarded_host:
        _append(f"{forwarded_proto}://{forwarded_host}")
    if host:
        _append(f"{forwarded_proto}://{host}")
        _append(f"{request.url.scheme}://{host}")

    _append(f"{request.url.scheme}://{request.url.netloc}")
    _append(f"https://{request.url.netloc}")
    return candidates


def _has_valid_twilio_signature(request: Request, form_items: list[tuple[str, str]]) -> bool:
    if not config.TWILIO_AUTH_TOKEN:
        return True

    provided = request.headers.get("X-Twilio-Signature", "").strip()
    if not provided:
        return False

    sorted_items = sorted(form_items, key=lambda item: item[0])
    for candidate_url in _candidate_request_urls(request):
        payload = [candidate_url]
        for key, value in sorted_items:
            payload.append(key)
            payload.append(value)

        digest = hmac.new(
            config.TWILIO_AUTH_TOKEN.encode("utf-8"),
            "".join(payload).encode("utf-8"),
            hashlib.sha1,
        ).digest()
        expected = base64.b64encode(digest).decode("utf-8")
        if hmac.compare_digest(expected, provided):
            return True
    return False


def _require_internal_bearer(request: Request) -> None:
    if not config.ACE_INTERNAL_API_TOKEN:
        raise HTTPException(status_code=500, detail="ACE_INTERNAL_API_TOKEN is not configured")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = header.split(" ", 1)[1].strip()
    if token != config.ACE_INTERNAL_API_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid internal bearer token")


def _require_linq_token(request: Request) -> None:
    if not config.LINQ_WEBHOOK_TOKEN:
        return
    provided = request.headers.get("x-linq-token") or request.headers.get("authorization", "")
    if provided.lower().startswith("bearer "):
        provided = provided.split(" ", 1)[1].strip()
    if provided.strip() != config.LINQ_WEBHOOK_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid Linq webhook token")


def _messaging_link(db, *, provider: str, address: str) -> Any | None:
    models_module, _, _, _ = _db_bundle()
    normalized = _normalize_address(address)
    if not normalized:
        return None
    stmt = select(models_module.MessagingLink).where(
        models_module.MessagingLink.provider == provider,
        models_module.MessagingLink.address == normalized,
    )
    return db.execute(stmt).scalar_one_or_none()


def _upsert_link(
    db,
    *,
    provider: str,
    channel: str,
    address: str,
    user_id: str,
    metadata: dict[str, Any] | None = None,
) -> Any:
    models_module, _, _, _ = _db_bundle()
    normalized = _normalize_address(address)
    link = _messaging_link(db, provider=provider, address=normalized)
    if link is None:
        link = models_module.MessagingLink(
            provider=provider,
            address=normalized,
            channel=channel,
            user_id=user_id,
            metadata_json=metadata or {},
        )
        db.add(link)
    else:
        link.user_id = user_id
        link.channel = channel
        link.metadata_json = metadata or {}
    db.commit()
    db.refresh(link)
    return link


async def _send_runtime_message(
    *,
    user_id: str,
    message: str,
    session_id: str,
    context: dict[str, Any] | None = None,
) -> str:
    try:
        runtime = await ensure_user_runtime(
            user_id,
            options=RuntimeOptions(allow_cached=True, wait_for_lock=True),
        )
    except RuntimeProvisioningError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    target_url = f"{runtime.upstream_url}/chat"
    payload = {
        "message": message,
        "session_id": session_id,
        "user_id": user_id,
        "context": context or {},
        "stream": False,
    }
    timeout = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            upstream = await client.post(target_url, json=payload)
    except httpx.TimeoutException as exc:
        raise HTTPException(status_code=504, detail="Runtime request timed out") from exc
    except httpx.ConnectError as exc:
        raise HTTPException(status_code=503, detail="Runtime connection failed") from exc

    if upstream.status_code >= 400:
        detail = upstream.text or f"upstream returned {upstream.status_code}"
        raise HTTPException(status_code=upstream.status_code, detail=detail)

    data = upstream.json()
    if isinstance(data, dict):
        for key in ("response", "content"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        tokens = data.get("tokens")
        if isinstance(tokens, list):
            return "".join(str(token) for token in tokens)
    raise HTTPException(status_code=502, detail="Runtime response was empty")


async def _post_linq_reply(reply_url: str, payload: dict[str, Any]) -> None:
    headers: dict[str, str] = {}
    if config.LINQ_API_KEY:
        if config.LINQ_API_KEY_HEADER.lower() == "authorization":
            headers["Authorization"] = f"Bearer {config.LINQ_API_KEY}"
        else:
            headers[config.LINQ_API_KEY_HEADER] = config.LINQ_API_KEY
    timeout = httpx.Timeout(config.LINQ_REPLY_TIMEOUT_SECONDS, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(reply_url, json=payload, headers=headers)
        resp.raise_for_status()


class AdminLinkRequest(BaseModel):
    provider: str
    address: str
    user_id: str
    channel: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinqInboundRequest(BaseModel):
    from_handle: Optional[str] = None
    sender: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    body: Optional[str] = None
    text: Optional[str] = None
    message: Optional[str] = None
    conversation_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    reply_url: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


@router.post("/admin/link")
async def admin_link(payload: AdminLinkRequest, request: Request) -> dict[str, Any]:
    _require_internal_bearer(request)
    ensure_messaging_tables()
    provider = (payload.provider or "").strip().lower()
    if provider not in {_TWILIO_PROVIDER, _LINQ_PROVIDER}:
        raise HTTPException(status_code=400, detail="Unsupported provider")
    channel = (payload.channel or (_TWILIO_CHANNEL if provider == _TWILIO_PROVIDER else _LINQ_CHANNEL)).strip().lower()
    _, session_local, _, _ = _db_bundle()
    db = session_local()
    try:
        link = _upsert_link(
            db,
            provider=provider,
            channel=channel,
            address=payload.address,
            user_id=payload.user_id,
            metadata=payload.metadata,
        )
        return {
            "provider": link.provider,
            "address": link.address,
            "channel": link.channel,
            "user_id": link.user_id,
            "metadata": link.metadata_json,
        }
    finally:
        db.close()


@router.get("/admin/link")
async def admin_get_link(provider: str, address: str, request: Request) -> dict[str, Any]:
    _require_internal_bearer(request)
    ensure_messaging_tables()
    _, session_local, _, _ = _db_bundle()
    db = session_local()
    try:
        link = _messaging_link(db, provider=provider.strip().lower(), address=address)
        if link is None:
            raise HTTPException(status_code=404, detail="Messaging link not found")
        return {
            "provider": link.provider,
            "address": link.address,
            "channel": link.channel,
            "user_id": link.user_id,
            "metadata": link.metadata_json,
        }
    finally:
        db.close()


@router.post("/twilio/whatsapp")
async def inbound_twilio_whatsapp(request: Request) -> Response:
    ensure_messaging_tables()
    form = await request.form()
    form_items = [(str(key), str(value)) for key, value in form.multi_items()]
    if not _has_valid_twilio_signature(request, form_items):
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    phone_number = _normalize_address(str(form.get("From") or ""))
    message = str(form.get("Body") or "").strip()
    if not phone_number:
        return _twiml_message("I could not determine your phone number. Please try again.")

    _, session_local, _, _ = _db_bundle()
    db = session_local()
    try:
        link = _messaging_link(db, provider=_TWILIO_PROVIDER, address=phone_number)
    finally:
        db.close()

    if link is None:
        if message.lower() in _RELINK_KEYWORDS:
            return _twiml_message("This WhatsApp number is not linked yet. Ask an admin to create the messaging link.")
        return _twiml_message("This WhatsApp number is not linked yet. Ask an admin to link it first.")

    if not message:
        return _twiml_message("Linked. Send a message here and I will reply.")

    context = {
        "provider": _TWILIO_PROVIDER,
        "channel": _TWILIO_CHANNEL,
        "from": phone_number,
        "received_at": _now_iso(),
    }
    reply = await _send_runtime_message(
        user_id=str(link.user_id),
        message=message,
        session_id=f"twilio:{phone_number}",
        context=context,
    )
    return _twiml_message(_clip_reply(reply, config.TWILIO_REPLY_CHAR_LIMIT))


@router.post("/linq/imessage")
async def inbound_linq_imessage(payload: LinqInboundRequest, request: Request) -> dict[str, Any]:
    ensure_messaging_tables()
    _require_linq_token(request)

    sender = _normalize_address(payload.from_handle or payload.sender or payload.from_)
    message = (payload.body or payload.text or payload.message or "").strip()
    conversation_id = (payload.conversation_id or payload.chat_id or payload.thread_id or sender).strip()
    if not sender:
        raise HTTPException(status_code=400, detail="Missing sender handle")
    if not message:
        return {"status": "ignored", "reason": "empty_message"}

    _, session_local, _, _ = _db_bundle()
    db = session_local()
    try:
        link = _messaging_link(db, provider=_LINQ_PROVIDER, address=sender)
    finally:
        db.close()

    if link is None:
        raise HTTPException(status_code=404, detail="iMessage handle is not linked")

    context = {
        "provider": _LINQ_PROVIDER,
        "channel": _LINQ_CHANNEL,
        "from": sender,
        "conversation_id": conversation_id,
        "metadata": payload.metadata,
        "received_at": _now_iso(),
    }
    reply = await _send_runtime_message(
        user_id=str(link.user_id),
        message=message,
        session_id=f"linq:{conversation_id}",
        context=context,
    )

    reply_payload = {
        "provider": _LINQ_PROVIDER,
        "channel": _LINQ_CHANNEL,
        "to": sender,
        "conversation_id": conversation_id,
        "reply": reply,
    }
    reply_url = (payload.reply_url or config.LINQ_REPLY_URL or "").strip()
    if reply_url:
        await _post_linq_reply(reply_url, reply_payload)
        return {"status": "ok", "reply_sent": True, **reply_payload}
    return {"status": "ok", "reply_sent": False, **reply_payload}
