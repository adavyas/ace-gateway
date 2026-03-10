from __future__ import annotations

import asyncio
from unittest.mock import Mock

import pytest

from ace_net_manager.services.health_service import HealthService
from ace_net_manager.settings import ManagerSettings


pytestmark = pytest.mark.unit


def _settings() -> ManagerSettings:
    return ManagerSettings(
        ace_internal_api_token="test-token",
        ace_agent_internal_port=8000,
        ace_container_startup_grace_seconds=0,
        ace_health_timeout_seconds=2,
        ace_health_poll_interval_seconds=1,
        ace_health_use_published_port_fallback=True,
    )


def test_candidate_bases_prefers_dns_and_adds_host_port_fallback():
    docker = Mock()
    docker.get_published_port.return_value = 18080
    service = HealthService(_settings(), docker_service=docker)
    service._running_in_container = Mock(return_value=False)  # type: ignore[method-assign]

    bases = service._candidate_bases("ace-u-user-g1")

    assert bases == ["http://ace-u-user-g1:8000", "http://127.0.0.1:18080"]


def test_candidate_bases_in_container_skips_host_port_fallback():
    docker = Mock()
    docker.get_published_port.return_value = 18080
    service = HealthService(_settings(), docker_service=docker)
    service._running_in_container = Mock(return_value=True)  # type: ignore[method-assign]

    bases = service._candidate_bases("ace-u-user-g1")

    assert bases == ["http://ace-u-user-g1:8000"]


def test_wait_until_ready_uses_fallback_when_dns_path_fails():
    docker = Mock()
    docker.get_published_port.return_value = 18080
    service = HealthService(_settings(), docker_service=docker)
    service._running_in_container = Mock(return_value=False)  # type: ignore[method-assign]

    seen: list[str] = []

    async def _fake_probe(_client, url: str) -> bool:
        seen.append(url)
        return url.startswith("http://127.0.0.1")

    service._probe = _fake_probe  # type: ignore[method-assign]

    ready = asyncio.run(
        service.wait_until_ready(
            "ace-u-user-g1",
            timeout_seconds=2,
            poll_interval_seconds=1,
            startup_grace_seconds=0,
        )
    )

    assert ready is True
    assert any(u.startswith("http://ace-u-user-g1") for u in seen)
    assert any(u.startswith("http://127.0.0.1:18080") for u in seen)
