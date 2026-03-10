from fastapi.testclient import TestClient

from gateway.main import app
from gateway.auth import deps
from gateway.auth.jwt import JWTValidationError


client = TestClient(app)


def test_me_missing_token_returns_401():
    response = client.get("/me")
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing bearer token"


def test_me_invalid_token_returns_401(monkeypatch):
    def _raise_invalid(_token: str):
        raise JWTValidationError("Invalid bearer token")

    monkeypatch.setattr(deps, "verify_supabase_jwt", _raise_invalid)
    response = client.get("/me", headers={"Authorization": "Bearer bad-token"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid bearer token"


def test_me_expired_token_returns_401(monkeypatch):
    def _raise_expired(_token: str):
        raise JWTValidationError("Token expired")

    monkeypatch.setattr(deps, "verify_supabase_jwt", _raise_expired)
    response = client.get("/me", headers={"Authorization": "Bearer expired-token"})

    assert response.status_code == 401
    assert response.json()["detail"] == "Token expired"


def test_me_valid_token_maps_sub_and_email(monkeypatch):
    def _valid(_token: str):
        return {"sub": "user-123", "email": "test@example.com"}

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid)
    response = client.get("/me", headers={"Authorization": "Bearer valid-token"})

    assert response.status_code == 200
    assert response.json() == {"user_id": "user-123", "email": "test@example.com"}


def test_notes_valid_token_uses_sub_as_owner_id(monkeypatch):
    def _valid(_token: str):
        return {"sub": "user-notes-1", "email": "note@example.com"}

    monkeypatch.setattr(deps, "verify_supabase_jwt", _valid)
    response = client.post(
        "/notes",
        headers={"Authorization": "Bearer valid-token"},
        json={"body": "note body"},
    )

    assert response.status_code == 200
    assert response.json() == {"owner_id": "user-notes-1", "body": "note body"}
