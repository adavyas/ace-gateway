from __future__ import annotations

from dataclasses import dataclass

from ace_net_manager.settings import ManagerSettings


@dataclass(frozen=True)
class RuntimeTimeouts:
    startup_grace_seconds: int
    health_timeout_seconds: int
    health_poll_interval_seconds: int
    drain_timeout_seconds: int


def build_timeouts(settings: ManagerSettings) -> RuntimeTimeouts:
    return RuntimeTimeouts(
        startup_grace_seconds=max(1, int(settings.ace_container_startup_grace_seconds)),
        health_timeout_seconds=max(5, int(settings.ace_health_timeout_seconds)),
        health_poll_interval_seconds=max(1, int(settings.ace_health_poll_interval_seconds)),
        drain_timeout_seconds=max(1, int(settings.ace_drain_timeout_seconds)),
    )

