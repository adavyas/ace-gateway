from types import SimpleNamespace

from fastapi.testclient import TestClient

from ace.gateway.main import app
from ace.gateway.routers import messaging as messaging_router


client = TestClient(app)


def _db_bundle_stub():
    return SimpleNamespace(), lambda: SimpleNamespace(close=lambda: None), None, None


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
    assert response.json()["reply_sent"] is True
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
