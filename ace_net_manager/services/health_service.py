from __future__ import annotations

import asyncio
import os
import time

import httpx

from ace_net_manager.settings import ManagerSettings


class HealthService:
    def __init__(self, settings: ManagerSettings, docker_service=None) -> None:
        self._settings = settings
        self._docker = docker_service

    def _base_url(self, container_name: str) -> str:
        return f"http://{container_name}:{int(self._settings.ace_agent_internal_port)}"

    def _running_in_container(self) -> bool:
        return os.path.exists("/.dockerenv")

    def _candidate_bases(self, container_name: str) -> list[str]:
        targets: list[str] = [self._base_url(container_name)]
        if (
            self._settings.ace_health_use_published_port_fallback
            and not self._running_in_container()
            and self._docker is not None
        ):
            host_port = self._docker.get_published_port(container_name, int(self._settings.ace_agent_internal_port))
            if host_port:
                targets.append(f"http://127.0.0.1:{int(host_port)}")
        # Keep deterministic probe order while removing duplicates.
        return list(dict.fromkeys(targets))

    async def _probe(self, client: httpx.AsyncClient, url: str) -> bool:
        try:
            resp = await client.get(url)
            return resp.status_code == 200
        except Exception:
            return False

    async def wait_until_ready(
        self,
        container_name: str,
        *,
        timeout_seconds: int | None = None,
        poll_interval_seconds: int | None = None,
        startup_grace_seconds: int | None = None,
    ) -> bool:
        timeout = int(timeout_seconds or self._settings.ace_health_timeout_seconds)
        poll_interval = int(poll_interval_seconds or self._settings.ace_health_poll_interval_seconds)
        startup_grace = int(startup_grace_seconds or self._settings.ace_container_startup_grace_seconds)

        if startup_grace > 0:
            await asyncio.sleep(startup_grace)

        deadline = time.monotonic() + max(1, timeout)
        async with httpx.AsyncClient(timeout=3.0) as client:
            while time.monotonic() < deadline:
                for base in self._candidate_bases(container_name):
                    is_healthy = await self._probe(client, f"{base}/healthz")
                    is_ready = await self._probe(client, f"{base}/readyz")
                    if is_healthy and is_ready:
                        return True
                await asyncio.sleep(max(1, poll_interval))

        return False
