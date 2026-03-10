from fastapi.testclient import TestClient

from gateway.main import app
from gateway.auth import deps
from gateway.auth.jwt import JWTValidationError
from gateway.routers import agent as agent_router
from gateway.routers import ace_proxy as ace_proxy_router
from gateway.routers import runtime as runtime_router
from gateway import runtime_manager as gateway_runtime_manager
from gateway.runtime_manager import RuntimeEnsureResult, RuntimeProvisioningError


client = TestClient(app)


def _valid_claims(_token: str) -> dict[str, str]:
    return {"sub": "11111111-2222-3333-4444-555555555555", "email": "user@example.com"}


def test_newuser_requires_auth():
    response = client.post("/newuser")
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token"


def test_newuser_invalid_token_returns_401(monkeypatch):
    def _invalid(_token: str):
        raise JWTValidationError("Invalid bearer token")

    monkeypatch.setattr(deps, "verify_supabase_jwt", _invalid)
    response = client.post("/newuser", headers={"Authorization": "Bearer bad-token"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token"


def test_newuser_uses_jwt_sub_and_returns_runtime_mapping(monkeypatch):
    captured: dict[str, object] = {}

    async def _ensure(user_id: str, *, options):
        captured["user_id"] = user_id
        captured["image_tag"] = options.image_tag
        captured["cpus"] = options.cpus
        captured["memory"] = options.memory
        return RuntimeEnsureResult(
            user_id=user_id,
            container_name="ace-user-11111111-2222-3333-4444-555555555555",
            volume_name="ace_user_11111111-2222-3333-4444-555555555555_data",
            upstream_url="http://ace-user-11111111-2222-3333-4444-555555555555:8080",
            status="running",
            image_ref="ace-hermes:latest",
            cached=False,
        )

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid_claims)
    monkeypatch.setattr(runtime_router, "ensure_user_runtime", _ensure)

    response = client.post(
        "/newuser",
        headers={"Authorization": "Bearer valid-token"},
        json={
            "image_tag": "latest",
            "resources": {"cpus": 1.5, "memory": "1g"},
        },
    )

    assert response.status_code == 200
    assert captured["user_id"] == "11111111-2222-3333-4444-555555555555"
    assert captured["image_tag"] == "latest"
    assert captured["cpus"] == 1.5
    assert captured["memory"] == "1g"
    assert response.json() == {
        "user_id": "11111111-2222-3333-4444-555555555555",
        "container_name": "ace-user-11111111-2222-3333-4444-555555555555",
        "volume_name": "ace_user_11111111-2222-3333-4444-555555555555_data",
        "upstream_url": "http://ace-user-11111111-2222-3333-4444-555555555555:8080",
        "status": "running",
        "image_ref": "ace-hermes:latest",
        "cached": False,
    }


def test_newuser_runtime_errors_passthrough(monkeypatch):
    async def _ensure(_user_id: str, *, options):
        raise RuntimeProvisioningError("Container is not ready yet; retry shortly", status_code=503)

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid_claims)
    monkeypatch.setattr(runtime_router, "ensure_user_runtime", _ensure)

    response = client.post("/newuser", headers={"Authorization": "Bearer valid-token"})
    assert response.status_code == 503
    assert response.json()["detail"] == "Container is not ready yet; retry shortly"


def test_gateway_runtime_image_ref_accepts_full_image_reference():
    assert gateway_runtime_manager._image_ref("ace-hermes:latest") == "ace-hermes:latest"
    assert gateway_runtime_manager._image_ref("ghcr.io/example/agent:canary") == "ghcr.io/example/agent:canary"


def test_agent_chat_requires_auth():
    response = client.post("/agent/chat", json={"message": "hello", "stream": False})
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token"


def test_agent_chat_valid_token_provisions_and_proxies(monkeypatch):
    runtime_call: dict[str, object] = {}
    upstream_call: dict[str, object] = {}

    async def _ensure(user_id: str, *, options):
        runtime_call["user_id"] = user_id
        runtime_call["allow_cached"] = options.allow_cached
        return RuntimeEnsureResult(
            user_id=user_id,
            container_name="ace-user-11111111-2222-3333-4444-555555555555",
            volume_name="ace_user_11111111-2222-3333-4444-555555555555_data",
            upstream_url="http://ace-user-11111111-2222-3333-4444-555555555555:8080",
            status="running",
            image_ref="ace-hermes:latest",
            cached=False,
        )

    class _FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"type": "done", "content": "ok"}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, json):
            upstream_call["url"] = url
            upstream_call["json"] = json
            return _FakeResponse()

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid_claims)
    monkeypatch.setattr(agent_router, "ensure_user_runtime", _ensure)
    monkeypatch.setattr(agent_router.httpx, "AsyncClient", _FakeAsyncClient)

    response = client.post(
        "/agent/chat",
        headers={"Authorization": "Bearer valid-token"},
        json={"message": "hello world", "stream": False, "context": {"x": 1}},
    )

    assert response.status_code == 200
    assert response.json() == {"type": "done", "content": "ok"}
    assert runtime_call == {
        "user_id": "11111111-2222-3333-4444-555555555555",
        "allow_cached": True,
    }
    assert upstream_call["url"] == "http://ace-user-11111111-2222-3333-4444-555555555555:8080/chat"
    assert upstream_call["json"] == {
        "message": "hello world",
        "session_id": None,
        "user_id": "11111111-2222-3333-4444-555555555555",
        "context": {"x": 1},
        "stream": False,
    }


def test_agent_chat_returns_runtime_error(monkeypatch):
    async def _ensure(_user_id: str, *, options):
        raise RuntimeProvisioningError("Provisioning already in progress for this user", status_code=409)

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid_claims)
    monkeypatch.setattr(agent_router, "ensure_user_runtime", _ensure)

    response = client.post(
        "/agent/chat",
        headers={"Authorization": "Bearer valid-token"},
        json={"message": "hello", "stream": False},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "Provisioning already in progress for this user"


def test_ace_proxy_requires_auth():
    response = client.post("/ace/v1/chat", json={"message": "hello"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token"


def test_ace_proxy_forwards_request_and_user_headers(monkeypatch):
    upstream_call: dict[str, object] = {}

    async def _ensure(user_id: str, *, options):
        return RuntimeEnsureResult(
            user_id=user_id,
            container_name="ace-user-11111111-2222-3333-4444-555555555555",
            volume_name="ace_user_11111111-2222-3333-4444-555555555555_data",
            upstream_url="http://ace-user-11111111-2222-3333-4444-555555555555:8080",
            status="running",
            image_ref="ace-hermes:latest",
            cached=False,
        )

    class _FakeResponse:
        status_code = 200
        headers = {"content-type": "application/json"}
        content = b'{"ok":true}'

        @staticmethod
        def json():
            return {"ok": True}

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, method, url, params, headers, content):
            upstream_call["method"] = method
            upstream_call["url"] = url
            upstream_call["params"] = params
            upstream_call["headers"] = headers
            upstream_call["content"] = content
            return _FakeResponse()

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid_claims)
    monkeypatch.setattr(ace_proxy_router, "ensure_user_runtime", _ensure)
    monkeypatch.setattr(ace_proxy_router.httpx, "AsyncClient", _FakeAsyncClient)

    response = client.post(
        "/ace/v1/chat",
        headers={"Authorization": "Bearer valid-token"},
        json={"message": "hello"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert upstream_call["method"] == "POST"
    assert upstream_call["url"] == "http://ace-user-11111111-2222-3333-4444-555555555555:8080/v1/chat"
    assert upstream_call["headers"]["x-ace-user-id"] == "11111111-2222-3333-4444-555555555555"
