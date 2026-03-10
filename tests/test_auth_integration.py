import base64
import json
import os
import time
import uuid

import pytest
import requests


def _required_env() -> tuple[str, str, str]:
    supabase_url = os.getenv("SUPABASE_URL", "").rstrip("/")
    supabase_publishable_key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "") or os.getenv("SUPABASE_ANON_KEY", "")
    supabase_secret_key = os.getenv("SUPABASE_SECRET_KEY", "") or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

    if not supabase_url or not supabase_publishable_key:
        pytest.skip("SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY are required for integration tests")

    return supabase_url, supabase_publishable_key, supabase_secret_key


def _decode_sub(token: str) -> str:
    parts = token.split(".")
    if len(parts) < 2:
        return ""
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload.encode()).decode()).get("sub", "")


def _create_confirmed_user_via_admin(supabase_url: str, secret_key: str, email: str, password: str) -> None:
    headers = {
        "apikey": secret_key,
        "Authorization": f"Bearer {secret_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        f"{supabase_url}/auth/v1/admin/users",
        headers=headers,
        json={"email": email, "password": password, "email_confirm": True},
        timeout=15,
    )

    # 422 can occur if the email already exists; login step will validate usability.
    if response.status_code not in (200, 201, 422):
        raise AssertionError(f"Admin user bootstrap failed ({response.status_code}): {response.text[:300]}")


def _signup_user_with_public_key(supabase_url: str, publishable_key: str, email: str, password: str) -> None:
    headers = {
        "apikey": publishable_key,
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        f"{supabase_url}/auth/v1/signup",
        headers=headers,
        json={"email": email, "password": password},
        timeout=15,
    )

    # Existing user or validation variants are acceptable for setup.
    if response.status_code not in (200, 201, 400, 422):
        raise AssertionError(f"Signup setup failed ({response.status_code}): {response.text[:300]}")


def _signup_and_login() -> tuple[str, str]:
    supabase_url, publishable_key, secret_key = _required_env()

    configured_email = os.getenv("TEST_USER_EMAIL", "").strip()
    email = configured_email or f"ace-auth-{int(time.time())}-{uuid.uuid4().hex[:8]}@example.com"
    password = os.getenv("TEST_USER_PASSWORD", "ScopeAuth123!")

    if secret_key:
        _create_confirmed_user_via_admin(supabase_url, secret_key, email, password)
    else:
        _signup_user_with_public_key(supabase_url, publishable_key, email, password)

    headers = {
        "apikey": publishable_key,
        "Authorization": f"Bearer {publishable_key}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        f"{supabase_url}/auth/v1/token?grant_type=password",
        headers=headers,
        json={"email": email, "password": password},
        timeout=15,
    )

    if response.status_code != 200:
        raise AssertionError(f"Supabase login failed ({response.status_code}): {response.text[:300]}")

    token = response.json().get("access_token", "")
    if not token:
        raise AssertionError("Supabase login response missing access_token")

    return token, _decode_sub(token)


@pytest.mark.integration
def test_me_integration_valid_token_returns_200_and_correct_sub():
    if os.getenv("RUN_SUPABASE_INTEGRATION", "0") != "1":
        pytest.skip("Set RUN_SUPABASE_INTEGRATION=1 to enable integration tests")

    api_base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
    token, token_sub = _signup_and_login()
    assert token_sub

    response = requests.get(
        f"{api_base_url}/me",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["user_id"] == token_sub


@pytest.mark.integration
def test_me_integration_without_token_returns_401():
    if os.getenv("RUN_SUPABASE_INTEGRATION", "0") != "1":
        pytest.skip("Set RUN_SUPABASE_INTEGRATION=1 to enable integration tests")

    api_base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
    response = requests.get(f"{api_base_url}/me", timeout=10)
    assert response.status_code == 401


@pytest.mark.integration
def test_me_integration_bad_token_returns_401():
    if os.getenv("RUN_SUPABASE_INTEGRATION", "0") != "1":
        pytest.skip("Set RUN_SUPABASE_INTEGRATION=1 to enable integration tests")

    api_base_url = os.getenv("API_BASE_URL", "http://127.0.0.1:8000")
    response = requests.get(
        f"{api_base_url}/me",
        headers={"Authorization": "Bearer not-a-real-token"},
        timeout=10,
    )
    assert response.status_code == 401
