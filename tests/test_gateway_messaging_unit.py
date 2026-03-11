from types import SimpleNamespace

from fastapi.testclient import TestClient

from gateway.main import app
from gateway.routers import messaging as messaging_router


client = TestClient(app)


def _db_bundle_stub():
    return SimpleNamespace(), lambda: SimpleNamespace(close=lambda: None), None, None


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDb:
    def __init__(self, value):
        self._value = value
        self.committed = False

    def execute(self, _stmt):
        return _ScalarResult(self._value)

    def commit(self):
        self.committed = True

    def close(self):
        return None


class _FakeSelect:
    def where(self, *_args, **_kwargs):
        return self


def test_admin_link_upserts_mapping(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_internal_bearer", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)

    def _upsert_link(db, **kwargs):
        return SimpleNamespace(
            provider=kwargs["provider"],
            address=kwargs["address"],
            channel=kwargs["channel"],
            user_id=kwargs["user_id"],
            metadata_json=kwargs["metadata"],
        )

    monkeypatch.setattr(messaging_router, "_upsert_link", _upsert_link)
    response = client.post(
        "/messaging/admin/link",
        headers={"Authorization": "Bearer internal-token"},
        json={
            "provider": "twilio_whatsapp",
            "address": "+15551234567",
            "user_id": "11111111-2222-3333-4444-555555555555",
            "metadata": {"label": "beta-user"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "twilio_whatsapp",
        "address": "+15551234567",
        "channel": "whatsapp",
        "user_id": "11111111-2222-3333-4444-555555555555",
        "metadata": {"label": "beta-user"},
    }


def test_twilio_whatsapp_inbound_routes_to_runtime(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_has_valid_twilio_signature", lambda request, form_items: True)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )
    async def _send_runtime_message(**kwargs):
        return "hello from runtime"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/twilio/whatsapp",
        data={"From": "whatsapp:+15551234567", "Body": "hello"},
    )

    assert response.status_code == 200
    assert "hello from runtime" in response.text
    assert response.headers["content-type"].startswith("application/xml")


def test_linq_imessage_returns_sync_reply(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )
    async def _send_runtime_message(**kwargs):
        return "imessage reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        json={
            "from": "+15551234567",
            "text": "hello there",
            "conversation_id": "chat-123",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "reply_sent": False,
        "provider": "linq_imessage",
        "channel": "imessage",
        "to": "+15551234567",
        "conversation_id": "chat-123",
        "reply": "imessage reply",
    }


def test_linq_imessage_posts_reply_when_callback_present(monkeypatch):
    posted = {}

    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )
    async def _send_runtime_message(**kwargs):
        return "callback reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)

    async def _post_linq_reply(reply_url, payload):
        posted["reply_url"] = reply_url
        posted["payload"] = payload

    monkeypatch.setattr(messaging_router, "_post_linq_reply", _post_linq_reply)
    response = client.post(
        "/messaging/linq/imessage",
        json={
            "from": "+15551234567",
            "body": "hello there",
            "thread_id": "chat-456",
            "reply_url": "https://linq.example/reply",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "reply_queued": True,
        "provider": "linq_imessage",
        "channel": "imessage",
        "to": "+15551234567",
        "conversation_id": "chat-456",
    }
    assert posted == {
        "reply_url": "https://linq.example/reply",
        "payload": {
            "provider": "linq_imessage",
            "channel": "imessage",
            "to": "+15551234567",
            "conversation_id": "chat-456",
            "reply": "callback reply",
        },
    }


def test_linq_imessage_unknown_sender_queues_onboarding_reply(monkeypatch):
    posted = {}

    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(messaging_router, "_messaging_link", lambda db, **kwargs: None)
    monkeypatch.setattr(
        messaging_router,
        "_create_linq_auth_session",
        lambda db, request, sender: (SimpleNamespace(id="auth-1"), "https://gateway.example.com/messaging/oauth/google/start?session=auth-1"),
    )

    async def _post_linq_reply(reply_url, payload):
        posted["reply_url"] = reply_url
        posted["payload"] = payload

    monkeypatch.setattr(messaging_router, "_post_linq_reply", _post_linq_reply)
    response = client.post(
        "/messaging/linq/imessage",
        json={
            "from": "+15551234567",
            "body": "hello there",
            "thread_id": "chat-unknown",
            "reply_url": "https://linq.example/reply",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "reply_queued": True,
        "provider": "linq_imessage",
        "channel": "imessage",
        "to": "+15551234567",
        "conversation_id": "chat-unknown",
        "onboarding_required": True,
    }
    assert posted["reply_url"] == "https://linq.example/reply"
    assert posted["payload"]["to"] == "+15551234567"
    assert "https://gateway.example.com/messaging/oauth/google/start?session=auth-1" in posted["payload"]["reply"]


def test_start_google_oauth_redirects_to_supabase(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "select", lambda *_args, **_kwargs: _FakeSelect())
    auth_session = SimpleNamespace(id="auth-1", is_expired=False, status="PENDING")
    db = _FakeDb(auth_session)
    models = SimpleNamespace(AuthSession=SimpleNamespace(id="id"))
    monkeypatch.setattr(
        messaging_router,
        "_db_bundle",
        lambda: (models, lambda: db, None, None),
    )
    monkeypatch.setattr(
        messaging_router,
        "_build_supabase_google_authorize_url",
        lambda session: f"https://supabase.example/auth/v1/authorize?session={session.id}",
    )

    response = client.get("/messaging/oauth/google/start?session=auth-1", follow_redirects=False)

    assert response.status_code == 307
    assert response.headers["location"] == "https://supabase.example/auth/v1/authorize?session=auth-1"


def test_google_oauth_callback_links_sender(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "select", lambda *_args, **_kwargs: _FakeSelect())
    auth_session = SimpleNamespace(
        id="auth-1",
        state="signed-state",
        is_expired=False,
        status="PENDING",
        pkce_verifier="verifier",
        redirect_uri="https://gateway.example.com/messaging/oauth/google/callback",
        scope_access_token=None,
        scope_refresh_token=None,
        error=None,
        delivered_at=None,
        user_id=None,
    )
    db = _FakeDb(auth_session)
    models = SimpleNamespace(AuthSession=SimpleNamespace(state="state"))
    monkeypatch.setattr(
        messaging_router,
        "_db_bundle",
        lambda: (models, lambda: db, None, None),
    )
    monkeypatch.setattr(
        messaging_router,
        "_decode_auth_state",
        lambda state: {"provider": "linq_imessage", "sender": "+15551234567"},
    )

    async def _exchange(_auth_session, _code):
        return {
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "user": {"id": "user-123", "email": "user@example.com"},
        }

    monkeypatch.setattr(messaging_router, "_exchange_supabase_pkce_code", _exchange)
    linked = {}

    def _upsert_link(db_obj, **kwargs):
        linked.update(kwargs)
        return SimpleNamespace(**kwargs)

    monkeypatch.setattr(messaging_router, "_upsert_link", _upsert_link)
    response = client.get("/messaging/oauth/google/callback?state=signed-state&code=oauth-code")

    assert response.status_code == 200
    assert "iMessage number is linked" in response.text
    assert linked == {
        "provider": "linq_imessage",
        "channel": "imessage",
        "address": "+15551234567",
        "user_id": "user-123",
        "metadata": {
            "linked_via": "google_oauth",
            "email": "user@example.com",
            "auth_session_id": "auth-1",
        },
    }
    assert auth_session.status == "COMPLETE"
    assert auth_session.user_id == "user-123"
    assert auth_session.scope_access_token == "access-token"
    assert auth_session.scope_refresh_token == "refresh-token"


def test_linq_imessage_accepts_nested_event_payload(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "nested reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        json={
            "event": "message.received",
            "data": {
                "senderHandle": "+15551234567",
                "text": "hello from nested payload",
                "conversationId": "chat-nested",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "nested reply"
    assert response.json()["conversation_id"] == "chat-nested"
    assert response.json()["to"] == "+15551234567"


def test_linq_imessage_accepts_logged_linq_payload_shape(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "logged-shape reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        json={
            "api_version": "v3",
            "event_type": "message.received",
            "data": {
                "chat": {"id": "b3d56891-458c-4fd9-9731-b2bf4bdd673d", "is_group": False},
                "parts": [{"type": "text", "value": "Hey"}],
                "sender_handle": {
                    "handle": "+19253070281",
                    "service": "iMessage",
                    "is_me": False,
                },
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "logged-shape reply"
    assert response.json()["conversation_id"] == "b3d56891-458c-4fd9-9731-b2bf4bdd673d"
    assert response.json()["to"] == "+19253070281"


def test_linq_imessage_accepts_camel_case_fields(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "camel reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        json={
            "senderHandle": "+15551234567",
            "content": "hello from camel case",
            "threadId": "chat-camel",
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "camel reply"
    assert response.json()["conversation_id"] == "chat-camel"
    assert response.json()["to"] == "+15551234567"


def test_linq_imessage_accepts_form_encoded_payload(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router, "_require_linq_token", lambda request: None)
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "form reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        data={
            "senderHandle": "+15551234567",
            "text": "hello from form",
            "conversationId": "chat-form",
        },
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "form reply"
    assert response.json()["conversation_id"] == "chat-form"
    assert response.json()["to"] == "+15551234567"


def test_linq_imessage_accepts_x_linq_token(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router.config, "LINQ_WEBHOOK_TOKEN", "sandbox-secret")
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "header reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        headers={"x-linq-token": "sandbox-secret"},
        json={"from": "+15551234567", "body": "hello", "thread_id": "chat-header"},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "header reply"


def test_linq_imessage_accepts_bearer_token(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router.config, "LINQ_WEBHOOK_TOKEN", "sandbox-secret")
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "bearer reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage",
        headers={"Authorization": "Bearer sandbox-secret"},
        json={"from": "+15551234567", "body": "hello", "thread_id": "chat-bearer"},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "bearer reply"


def test_linq_imessage_accepts_query_token(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router.config, "LINQ_WEBHOOK_TOKEN", "sandbox-secret")
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "query reply"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage?token=sandbox-secret",
        json={"from": "+15551234567", "body": "hello", "thread_id": "chat-query"},
    )

    assert response.status_code == 200
    assert response.json()["reply"] == "query reply"


def test_linq_imessage_prefers_header_over_query_token(monkeypatch):
    monkeypatch.setattr(messaging_router, "ensure_messaging_tables", lambda: None)
    monkeypatch.setattr(messaging_router.config, "LINQ_WEBHOOK_TOKEN", "sandbox-secret")
    monkeypatch.setattr(messaging_router, "_db_bundle", _db_bundle_stub)
    monkeypatch.setattr(
        messaging_router,
        "_messaging_link",
        lambda db, **kwargs: SimpleNamespace(user_id="11111111-2222-3333-4444-555555555555"),
    )

    async def _send_runtime_message(**kwargs):
        return "should not reach runtime"

    monkeypatch.setattr(messaging_router, "_send_runtime_message", _send_runtime_message)
    response = client.post(
        "/messaging/linq/imessage?token=sandbox-secret",
        headers={"x-linq-token": "wrong-secret"},
        json={"from": "+15551234567", "body": "hello", "thread_id": "chat-precedence"},
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Invalid Linq webhook token"}
