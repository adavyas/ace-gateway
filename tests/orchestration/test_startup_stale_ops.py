from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from ace_net_manager.main import create_app
from ace_net_manager.settings import ManagerSettings, get_settings


pytestmark = pytest.mark.unit


class _DummyDb:
    def close(self) -> None:
        return


class _FakeRunner:
    async def start(self) -> None:
        return

    async def shutdown(self) -> None:
        return


class _FakeOperationService:
    def __init__(self) -> None:
        self.calls: list[int] = []

    def fail_stale_inflight_operations(self, _db, *, stale_minutes: int) -> int:
        self.calls.append(stale_minutes)
        return 0


@dataclass
class _Bundle:
    settings: ManagerSettings
    runtime_service: object
    operation_service: _FakeOperationService
    operation_runner: _FakeRunner


def test_startup_sweeps_stale_operations(monkeypatch):
    monkeypatch.setenv("ACE_INTERNAL_API_TOKEN", "test-token")
    monkeypatch.setenv("ACE_STALE_OPERATION_GRACE_MINUTES", "7")
    get_settings.cache_clear()

    import ace_net_manager.main as main_module

    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "SessionLocal", lambda: _DummyDb())

    op_service = _FakeOperationService()
    bundle = _Bundle(
        settings=ManagerSettings(ace_internal_api_token="test-token", ace_stale_operation_grace_minutes=7),
        runtime_service=object(),
        operation_service=op_service,
        operation_runner=_FakeRunner(),
    )

    app = create_app(
        service_bundle=bundle,
        init_database_on_start=True,
        start_runner_on_start=False,
    )

    with TestClient(app) as _client:
        pass

    assert op_service.calls == [7]
