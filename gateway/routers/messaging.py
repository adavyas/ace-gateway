from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from datetime import datetime
from typing import Any, Optional
from urllib.parse import parse_qsl, urlencode
from xml.sax.saxutils import escape

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from .. import config
from ..auth.jwt import JWTValidationError, verify_supabase_jwt
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
    db_base.metadata.create_all(
        bind=db_engine,
        tables=[
            models_module.MessagingLink.__table__,
            models_module.AuthSession.__table__,
        ],
    )


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


def _public_base_url(request: Request) -> str:
    if config.PUBLIC_BASE_URL:
        return config.PUBLIC_BASE_URL
    return str(request.base_url).rstrip("/")


def _absolute_url(request: Request, path: str, **query: str) -> str:
    base = _public_base_url(request)
    url = f"{base}{path}"
    filtered = {key: value for key, value in query.items() if value}
    if filtered:
        url = f"{url}?{urlencode(filtered)}"
    return url


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

    provided = request.headers.get("x-linq-token", "").strip()
    if not provided:
        authorization = request.headers.get("authorization", "").strip()
        if authorization.lower().startswith("bearer "):
            provided = authorization.split(" ", 1)[1].strip()
        elif not authorization:
            provided = request.query_params.get("token", "").strip()

    if provided != config.LINQ_WEBHOOK_TOKEN:
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


def _pkce_verifier() -> str:
    verifier = secrets.token_urlsafe(48)
    return verifier[:128]


def _urlsafe_b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _urlsafe_b64decode(raw: str) -> bytes:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    return _urlsafe_b64encode(digest)


def _encode_auth_state(*, session_id: str, provider: str, sender: str) -> str:
    payload = {
        "sid": session_id,
        "provider": provider,
        "sender": sender,
        "iat": int(time.time()),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _urlsafe_b64encode(raw)
    sig = hmac.new(
        config.AUTH_STATE_SIGNING_KEY.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return f"{body}.{_urlsafe_b64encode(sig)}"


def _decode_auth_state(state: str) -> dict[str, Any]:
    body, _, sig = (state or "").partition(".")
    if not body or not sig:
        raise HTTPException(status_code=400, detail="Invalid auth state")
    expected_sig = hmac.new(
        config.AUTH_STATE_SIGNING_KEY.encode("utf-8"),
        body.encode("ascii"),
        hashlib.sha256,
    ).digest()
    try:
        provided_sig = _urlsafe_b64decode(sig)
        payload = json.loads(_urlsafe_b64decode(body).decode("utf-8"))
    except Exception as exc:  # pragma: no cover - defensive parsing
        raise HTTPException(status_code=400, detail="Invalid auth state") from exc
    if not hmac.compare_digest(expected_sig, provided_sig):
        raise HTTPException(status_code=400, detail="Invalid auth state")
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid auth state")
    return payload


def _build_linq_reply_payload(sender: str, conversation_id: str, reply: str) -> dict[str, Any]:
    return {
        "provider": _LINQ_PROVIDER,
        "channel": _LINQ_CHANNEL,
        "to": sender,
        "conversation_id": conversation_id,
        "reply": reply,
    }


def _build_linq_ack_payload(sender: str, conversation_id: str, *, onboarding_required: bool = False) -> dict[str, Any]:
    payload = {
        "status": "accepted",
        "reply_queued": True,
        "provider": _LINQ_PROVIDER,
        "channel": _LINQ_CHANNEL,
        "to": sender,
        "conversation_id": conversation_id,
    }
    if onboarding_required:
        payload["onboarding_required"] = True
    return payload


def _linq_onboarding_message(start_url: str) -> str:
    minutes = max(int(config.AUTH_SESSION_TTL_SECONDS // 60), 1)
    return (
        "To link this iMessage number, sign in with Google here: "
        f"{start_url} "
        f"This link expires in about {minutes} minutes."
    )


def _create_linq_auth_session(db, request: Request, sender: str) -> tuple[Any, str]:
    models_module, _, _, _ = _db_bundle()
    session_id = uuid.uuid4().hex
    redirect_uri = _absolute_url(request, "/messaging/oauth/google/callback")
    verifier = _pkce_verifier()
    state = _encode_auth_state(session_id=session_id, provider=_LINQ_PROVIDER, sender=sender)
    auth_session = models_module.AuthSession(
        id=session_id,
        status="PENDING",
        state=state,
        pkce_verifier=verifier,
        redirect_uri=redirect_uri,
        scopes=config.GOOGLE_OAUTH_SCOPES,
        expires_at_epoch=int(time.time()) + max(config.AUTH_SESSION_TTL_SECONDS, 60),
    )
    db.add(auth_session)
    db.commit()
    db.refresh(auth_session)
    return auth_session, _absolute_url(request, "/messaging/oauth/google/start", session=session_id)


def _build_supabase_google_authorize_url(auth_session: Any) -> str:
    if not config.SUPABASE_URL or not config.SUPABASE_PUBLISHABLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase OAuth is not configured")
    query = urlencode(
        {
            "provider": "google",
            "redirect_to": auth_session.redirect_uri,
            "state": auth_session.state,
            "code_challenge": _pkce_challenge(str(auth_session.pkce_verifier)),
            "code_challenge_method": "S256",
            "scopes": str(auth_session.scopes or config.GOOGLE_OAUTH_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{config.SUPABASE_URL}/auth/v1/authorize?{query}"


async def _exchange_supabase_pkce_code(auth_session: Any, code: str) -> dict[str, Any]:
    if not config.SUPABASE_URL or not config.SUPABASE_PUBLISHABLE_KEY:
        raise HTTPException(status_code=500, detail="Supabase OAuth is not configured")

    headers = {
        "apikey": config.SUPABASE_PUBLISHABLE_KEY,
        "Authorization": f"Bearer {config.SUPABASE_PUBLISHABLE_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "auth_code": code,
        "code_verifier": str(auth_session.pkce_verifier),
        "redirect_to": str(auth_session.redirect_uri),
    }
    async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
        response = await client.post(
            f"{config.SUPABASE_URL}/auth/v1/token?grant_type=pkce",
            headers=headers,
            json=payload,
        )
    if response.status_code >= 400:
        detail = response.text[:500] or "Supabase OAuth exchange failed"
        raise HTTPException(status_code=502, detail=detail)
    data = response.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=502, detail="Supabase OAuth response was invalid")
    return data


def _exchange_access_token(data: dict[str, Any]) -> str:
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    return _first_non_empty_str(data.get("access_token"), session.get("access_token"))


def _exchange_refresh_token(data: dict[str, Any]) -> str:
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    return _first_non_empty_str(data.get("refresh_token"), session.get("refresh_token"))


def _exchange_user(data: dict[str, Any], access_token: str) -> tuple[str, str]:
    session = data.get("session") if isinstance(data.get("session"), dict) else {}
    user = data.get("user") if isinstance(data.get("user"), dict) else {}
    if not user and isinstance(session.get("user"), dict):
        user = session.get("user")

    user_id = _first_non_empty_str(user.get("id"))
    email = _first_non_empty_str(user.get("email"))
    if user_id:
        return user_id, email

    if access_token:
        try:
            claims = verify_supabase_jwt(access_token)
            return str(claims.get("sub") or ""), _first_non_empty_str(claims.get("email"))
        except JWTValidationError:
            pass

    raise HTTPException(status_code=502, detail="Supabase OAuth response missing user id")


def _oauth_status_page(message: str, *, status_code: int = 200) -> Response:
    html = (
        "<html><body style=\"font-family: sans-serif; max-width: 560px; margin: 48px auto;\">"
        f"<p>{escape(message)}</p>"
        "</body></html>"
    )
    return Response(content=html, media_type="text/html", status_code=status_code)


async def _process_linq_inbound_reply(
    *,
    user_id: str,
    sender: str,
    conversation_id: str,
    message: str,
    context: dict[str, Any],
    reply_url: str,
) -> None:
    try:
        reply = await _send_runtime_message(
            user_id=user_id,
            message=message,
            session_id=f"linq:{conversation_id}",
            context=context,
        )
        reply_payload = _build_linq_reply_payload(sender, conversation_id, reply)
        await _post_linq_reply(reply_url, reply_payload)
    except Exception:
        logger.exception(
            "Linq inbound background reply failed sender=%s conversation_id=%s",
            sender,
            conversation_id,
        )


class AdminLinkRequest(BaseModel):
    provider: str
    address: str
    user_id: str
    channel: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LinqInboundRequest(BaseModel):
    from_handle: Optional[str] = None
    sender: Optional[str] = None
    sender_handle: Optional[str] = None
    senderHandle: Optional[str] = None
    from_: Optional[str] = Field(default=None, alias="from")
    body: Optional[str] = None
    text: Optional[str] = None
    message: Optional[Any] = None
    content: Optional[str] = None
    conversation_id: Optional[str] = None
    chat_id: Optional[str] = None
    thread_id: Optional[str] = None
    conversationId: Optional[str] = None
    chatId: Optional[str] = None
    threadId: Optional[str] = None
    reply_url: Optional[str] = None
    replyUrl: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    event: Optional[str] = None
    data: dict[str, Any] = Field(default_factory=dict)

    model_config = {"populate_by_name": True}


def _first_non_empty_str(*values: Any) -> str:
    for value in values:
        if isinstance(value, str):
            cleaned = value.strip()
            if cleaned:
                return cleaned
    return ""


def _extract_handle(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return _first_non_empty_str(
            value.get("handle"),
            value.get("address"),
            value.get("value"),
            value.get("id"),
        )
    return ""


def _extract_parts_text(parts: Any) -> str:
    if not isinstance(parts, list):
        return ""
    values: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = _first_non_empty_str(part.get("value"), part.get("text"), part.get("content"))
            if text:
                values.append(text)
        elif isinstance(part, str) and part.strip():
            values.append(part.strip())
    return " ".join(values).strip()


def _linq_message_text(payload: LinqInboundRequest) -> str:
    message_value = payload.message
    nested_message = payload.data if isinstance(payload.data, dict) else {}
    if isinstance(message_value, dict):
        nested_message = {**nested_message, **message_value}
    chat = nested_message.get("chat") if isinstance(nested_message.get("chat"), dict) else {}

    return _first_non_empty_str(
        payload.body,
        payload.text,
        payload.content,
        message_value if isinstance(message_value, str) else "",
        nested_message.get("body"),
        nested_message.get("text"),
        nested_message.get("message"),
        nested_message.get("content"),
        _extract_parts_text(nested_message.get("parts")),
        _extract_parts_text(chat.get("parts")),
    )


def _linq_sender(payload: LinqInboundRequest) -> str:
    nested = payload.data if isinstance(payload.data, dict) else {}
    message_value = payload.message if isinstance(payload.message, dict) else {}
    return _normalize_address(
        _first_non_empty_str(
            payload.from_handle,
            payload.sender,
            payload.sender_handle,
            payload.senderHandle,
            payload.from_,
            _extract_handle(nested.get("sender_handle")),
            _extract_handle(nested.get("senderHandle")),
            _extract_handle(nested.get("from_handle")),
            _extract_handle(nested.get("from")),
            nested.get("from_handle"),
            nested.get("sender"),
            nested.get("sender_handle"),
            nested.get("senderHandle"),
            nested.get("from"),
            _extract_handle(message_value.get("sender_handle")),
            _extract_handle(message_value.get("senderHandle")),
            _extract_handle(message_value.get("from_handle")),
            _extract_handle(message_value.get("from")),
            message_value.get("from_handle"),
            message_value.get("sender"),
            message_value.get("sender_handle"),
            message_value.get("senderHandle"),
            message_value.get("from"),
        )
    )


def _linq_conversation_id(payload: LinqInboundRequest, sender: str) -> str:
    nested = payload.data if isinstance(payload.data, dict) else {}
    message_value = payload.message if isinstance(payload.message, dict) else {}
    chat = nested.get("chat") if isinstance(nested.get("chat"), dict) else {}
    return _first_non_empty_str(
        payload.conversation_id,
        payload.chat_id,
        payload.thread_id,
        payload.conversationId,
        payload.chatId,
        payload.threadId,
        nested.get("conversation_id"),
        nested.get("chat_id"),
        nested.get("thread_id"),
        nested.get("conversationId"),
        nested.get("chatId"),
        nested.get("threadId"),
        chat.get("id"),
        message_value.get("conversation_id"),
        message_value.get("chat_id"),
        message_value.get("thread_id"),
        message_value.get("conversationId"),
        message_value.get("chatId"),
        message_value.get("threadId"),
        sender,
    )


def _linq_reply_url(payload: LinqInboundRequest) -> str:
    nested = payload.data if isinstance(payload.data, dict) else {}
    message_value = payload.message if isinstance(payload.message, dict) else {}
    return _first_non_empty_str(
        payload.reply_url,
        payload.replyUrl,
        nested.get("reply_url"),
        nested.get("replyUrl"),
        message_value.get("reply_url"),
        message_value.get("replyUrl"),
        config.LINQ_REPLY_URL,
    )


def _linq_metadata(payload: LinqInboundRequest) -> dict[str, Any]:
    nested = payload.data if isinstance(payload.data, dict) else {}
    metadata = dict(payload.metadata or {})
    if payload.event:
        metadata.setdefault("event", payload.event)
    if nested:
        metadata.setdefault("data", nested)
    return metadata


async def _parse_linq_request_payload(request: Request) -> dict[str, Any]:
    content_type = (request.headers.get("content-type") or "").lower()
    body = await request.body()
    if not body:
        return {}

    parsed: Any = None
    text = body.decode("utf-8", errors="replace")

    if "application/json" in content_type or not content_type:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None

    if parsed is None and (
        "application/x-www-form-urlencoded" in content_type
        or "multipart/form-data" in content_type
    ):
        form = await request.form()
        parsed = {}
        for key, value in form.multi_items():
            parsed[str(key)] = str(value)

    if parsed is None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = dict(parse_qsl(text, keep_blank_values=True))

    if isinstance(parsed, dict):
        return parsed
    return {"message": parsed}


def _payload_preview(payload_dict: dict[str, Any]) -> str:
    try:
        raw = json.dumps(payload_dict, ensure_ascii=False)
    except Exception:
        raw = repr(payload_dict)
    return raw[:1000]


@router.get("/oauth/google/start")
async def start_google_oauth(session: str) -> Response:
    ensure_messaging_tables()
    models_module, session_local, _, _ = _db_bundle()
    db = session_local()
    try:
        auth_session = db.execute(
            select(models_module.AuthSession).where(models_module.AuthSession.id == session)
        ).scalar_one_or_none()
        if auth_session is None:
            raise HTTPException(status_code=404, detail="Auth session not found")
        if auth_session.is_expired:
            auth_session.status = "EXPIRED"
            db.commit()
            return _oauth_status_page("This sign-in link has expired. Send another message to get a new one.", status_code=410)
        if str(auth_session.status or "").upper() == "COMPLETE":
            return _oauth_status_page("This iMessage number is already linked. Return to Messages and send your message again.")
        return RedirectResponse(_build_supabase_google_authorize_url(auth_session), status_code=307)
    finally:
        db.close()


@router.get("/oauth/google/callback")
async def google_oauth_callback(
    state: str,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> Response:
    ensure_messaging_tables()
    models_module, session_local, _, _ = _db_bundle()
    db = session_local()
    try:
        auth_session = db.execute(
            select(models_module.AuthSession).where(models_module.AuthSession.state == state)
        ).scalar_one_or_none()
        if auth_session is None:
            raise HTTPException(status_code=404, detail="Auth session not found")

        state_payload = _decode_auth_state(state)
        sender = _normalize_address(str(state_payload.get("sender") or ""))
        provider = str(state_payload.get("provider") or "")
        if provider != _LINQ_PROVIDER or not sender:
            raise HTTPException(status_code=400, detail="Invalid auth session payload")

        if auth_session.is_expired:
            auth_session.status = "EXPIRED"
            db.commit()
            return _oauth_status_page("This sign-in link has expired. Send another message to get a new one.", status_code=410)

        if error:
            auth_session.status = "FAILED"
            auth_session.error = _first_non_empty_str(error_description, error)
            db.commit()
            return _oauth_status_page("Google sign-in was cancelled or failed. Send another message to try again.", status_code=400)

        if not code:
            auth_session.status = "FAILED"
            auth_session.error = "Missing OAuth code"
            db.commit()
            raise HTTPException(status_code=400, detail="Missing OAuth code")

        try:
            token_data = await _exchange_supabase_pkce_code(auth_session, code)
        except HTTPException as exc:
            auth_session.status = "FAILED"
            auth_session.error = exc.detail if isinstance(exc.detail, str) else "OAuth exchange failed"
            db.commit()
            return _oauth_status_page("Google sign-in could not be completed. Send another message to try again.", status_code=400)
        access_token = _exchange_access_token(token_data)
        refresh_token = _exchange_refresh_token(token_data)
        user_id, email = _exchange_user(token_data, access_token)

        auth_session.status = "COMPLETE"
        auth_session.user_id = user_id
        auth_session.scope_access_token = access_token or None
        auth_session.scope_refresh_token = refresh_token or None
        auth_session.error = None
        auth_session.delivered_at = datetime.utcnow()
        _upsert_link(
            db,
            provider=_LINQ_PROVIDER,
            channel=_LINQ_CHANNEL,
            address=sender,
            user_id=user_id,
            metadata={
                "linked_via": "google_oauth",
                "email": email,
                "auth_session_id": str(auth_session.id),
            },
        )
        return _oauth_status_page("Your iMessage number is linked. Return to Messages and send your message again.")
    except HTTPException as exc:
        if exc.status_code >= 500:
            logger.exception("Google OAuth callback failed")
        raise
    finally:
        db.close()


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
async def inbound_linq_imessage(
    request: Request,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    ensure_messaging_tables()
    _require_linq_token(request)
    payload_dict = await _parse_linq_request_payload(request)
    try:
        payload = LinqInboundRequest.model_validate(payload_dict)
    except ValidationError as exc:
        logger.warning("Linq inbound payload validation failed: %s payload=%s", exc, _payload_preview(payload_dict))
        raise HTTPException(status_code=400, detail="Invalid Linq webhook payload") from exc

    sender = _linq_sender(payload)
    message = _linq_message_text(payload)
    conversation_id = _linq_conversation_id(payload, sender)
    if not sender:
        logger.warning("Linq inbound missing sender handle payload=%s", _payload_preview(payload_dict))
        raise HTTPException(status_code=400, detail="Missing sender handle")
    if not message:
        logger.info("Linq inbound empty message sender=%s payload=%s", sender, _payload_preview(payload_dict))
        return {"status": "ignored", "reason": "empty_message"}

    _, session_local, _, _ = _db_bundle()
    reply_url = _linq_reply_url(payload)
    db = session_local()
    try:
        link = _messaging_link(db, provider=_LINQ_PROVIDER, address=sender)
        if link is None or message.lower() in _RELINK_KEYWORDS:
            _, start_url = _create_linq_auth_session(db, request, sender)
            reply_payload = _build_linq_reply_payload(
                sender,
                conversation_id,
                _linq_onboarding_message(start_url),
            )
            if reply_url:
                background_tasks.add_task(_post_linq_reply, reply_url, reply_payload)
                return _build_linq_ack_payload(sender, conversation_id, onboarding_required=True)
            return {"status": "ok", "reply_sent": False, "onboarding_required": True, **reply_payload}
    finally:
        db.close()

    context = {
        "provider": _LINQ_PROVIDER,
        "channel": _LINQ_CHANNEL,
        "from": sender,
        "conversation_id": conversation_id,
        "metadata": _linq_metadata(payload),
        "received_at": _now_iso(),
    }
    if reply_url:
        background_tasks.add_task(
            _process_linq_inbound_reply,
            user_id=str(link.user_id),
            sender=sender,
            conversation_id=conversation_id,
            message=message,
            context=context,
            reply_url=reply_url,
        )
        return _build_linq_ack_payload(sender, conversation_id)

    reply = await _send_runtime_message(
        user_id=str(link.user_id),
        message=message,
        session_id=f"linq:{conversation_id}",
        context=context,
    )
    reply_payload = _build_linq_reply_payload(sender, conversation_id, reply)
    return {"status": "ok", "reply_sent": False, **reply_payload}
