from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ace_net_manager.main import create_app
from ace_net_manager.settings import ManagerSettings, get_settings


pytestmark = pytest.mark.unit


class _DummyDb:
    def close(self) -> None:
        return


class _FakeRunner:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def start(self) -> None:
        return

    async def shutdown(self) -> None:
        return

    async def enqueue(self, operation_id: str) -> None:
        self.enqueued.append(operation_id)


class _FakeOperationService:
    def __init__(self) -> None:
        self._ops: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self.inflight = False

    def has_inflight_operation(self, db, user_id: str) -> bool:
        return self.inflight

    def create_operation(
        self,
        db,
        *,
        user_id: str,
        operation_type: str,
        requested_by: str,
        requested_image_tag: str | None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        self._counter += 1
        op_id = f"op-{self._counter}"
        self._ops[op_id] = {
            "operation_id": op_id,
            "user_id": user_id,
            "operation_type": operation_type,
            "status": "queued",
            "step": "queued",
            "requested_image_tag": requested_image_tag,
            "metadata": metadata or {},
            "created_at": "2026-01-01T00:00:00Z",
        }
        return op_id

    def get_operation(self, db, operation_id: str):
        return self._ops.get(operation_id)

    def serialize_operation(self, row: dict[str, Any]) -> dict[str, Any]:
        return row


class _FakeRuntimeService:
    def __init__(self) -> None:
        self.runtime_row: dict[str, Any] | None = {
            "user_id": "11111111-1111-1111-1111-111111111111",
            "status": "active",
            "current_generation": 7,
            "active_container_name": "ace-u-user-g7",
            "active_image_tag": "ace-agent:v2",
            "previous_generation": 6,
            "previous_image_tag": "ace-agent:v1",
            "last_health_status": "healthy",
            "last_health_checked_at": "2026-01-01T00:00:01Z",
            "last_operation_id": "op-1",
        }

    def get_runtime(self, db, user_id: str):
        if self.runtime_row and self.runtime_row.get("user_id") == user_id:
            return self.runtime_row
        return None


@dataclass
class _Bundle:
    settings: ManagerSettings
    runtime_service: _FakeRuntimeService
    operation_service: _FakeOperationService
    operation_runner: _FakeRunner


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("ACE_INTERNAL_API_TOKEN", "test-token")
    get_settings.cache_clear()

    from ace_net_manager.api import routes as routes_module

    monkeypatch.setattr(routes_module, "SessionLocal", lambda: _DummyDb())

    op_service = _FakeOperationService()
    runtime_service = _FakeRuntimeService()
    runner = _FakeRunner()
    settings = ManagerSettings(ace_internal_api_token="test-token")
    bundle = _Bundle(
        settings=settings,
        runtime_service=runtime_service,
        operation_service=op_service,
        operation_runner=runner,
    )
    app = create_app(
        service_bundle=bundle,
        init_database_on_start=False,
        start_runner_on_start=False,
    )
    return TestClient(app), bundle


def test_internal_auth_required(client):
    test_client, _ = client
    resp = test_client.post(
        "/new-user",
        json={"user_id": "11111111-1111-1111-1111-111111111111", "image_tag": "ace-agent:v1"},
    )
    assert resp.status_code == 401


def test_new_user_returns_202_and_enqueues_operation(client):
    test_client, bundle = client
    resp = test_client.post(
        "/new-user",
        headers={"Authorization": "Bearer test-token"},
        json={"user_id": "11111111-1111-1111-1111-111111111111", "image_tag": "ace-agent:v1"},
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["operation_id"] in bundle.operation_runner.enqueued


def test_conflict_when_inflight_operation_exists(client):
    test_client, bundle = client
    bundle.operation_service.inflight = True
    resp = test_client.post(
        "/deploy/11111111-1111-1111-1111-111111111111",
        headers={"Authorization": "Bearer test-token"},
        json={"image_tag": "ace-agent:v2"},
    )
    assert resp.status_code == 409


def test_get_status_returns_runtime_projection(client):
    test_client, _ = client
    resp = test_client.get(
        "/status/11111111-1111-1111-1111-111111111111",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["runtime_status"] == "active"
    assert data["current_generation"] == 7
    assert data["active_container_name"] == "ace-u-user-g7"


def test_get_operation_status(client):
    test_client, bundle = client
    _ = test_client.post(
        "/deploy/11111111-1111-1111-1111-111111111111",
        headers={"Authorization": "Bearer test-token"},
        json={"image_tag": "ace-agent:v2"},
    )
    op_id = bundle.operation_runner.enqueued[-1]

    resp = test_client.get(
        f"/operations/{op_id}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["operation_id"] == op_id
    assert data["operation_type"] == "deploy"
    assert data["status"] == "queued"



def test_new_user_metadata_is_json_safe(client):
    test_client, bundle = client
    resp = test_client.post(
        "/new-user",
        headers={"Authorization": "Bearer test-token"},
        json={"user_id": "22222222-2222-2222-2222-222222222222", "image_tag": "ace-agent:v1"},
    )
    assert resp.status_code == 202
    op_id = resp.json()["operation_id"]
    metadata = bundle.operation_service._ops[op_id]["metadata"]
    assert metadata["user_id"] == "22222222-2222-2222-2222-222222222222"
    assert isinstance(metadata["user_id"], str)
