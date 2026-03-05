"""
tunnel.py - SSH tunnel manager for ACE backend VPS connectivity.

This module provides a TunnelManager singleton that maintains a persistent
SSH tunnel from a dynamically chosen local port to the ACE HTTP backend
running on the VPS. It handles reconnection with exponential back-off and
runs a background asyncio task that periodically verifies tunnel health.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Optional

import httpx

from . import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return an ephemeral TCP port that is currently unbound on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return s.getsockname()[1]


def _expand_key_path(path: str) -> str:
    """Expand ~ and environment variables in the SSH key path."""
    return os.path.expandvars(os.path.expanduser(path))


# ---------------------------------------------------------------------------
# TunnelManager
# ---------------------------------------------------------------------------


class TunnelManager:
    """Singleton that manages an SSH port-forward tunnel to the ACE VPS.

    The tunnel is opened with:
        ssh -N -o StrictHostKeyChecking=accept-new
            -i <key>
            -L <local_port>:localhost:<vps_ace_port>
            <user>@<host>

    A background asyncio task pings the tunnel endpoint every
    ``config.TUNNEL_RECONNECT_INTERVAL`` seconds and restarts it if needed.

    Usage::

        manager = TunnelManager.get_instance()
        await manager.start()          # called once at app startup
        await manager.ensure_running() # called before each proxy request
        await manager.stop()           # called at app shutdown
    """

    _instance: Optional["TunnelManager"] = None
    _instance_lock: asyncio.Lock  # created lazily per event-loop

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "TunnelManager":
        """Return (and lazily create) the process-wide singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        self._local_port: Optional[int] = None
        self._process: Optional[asyncio.subprocess.Process] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._start_lock: Optional[asyncio.Lock] = None  # lazy, per event-loop
        self._running: bool = False
        self._backoff: float = 1.0  # current reconnect delay in seconds

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def _lock(self) -> asyncio.Lock:
        """Lazily create the per-loop asyncio lock on first access."""
        if self._start_lock is None:
            self._start_lock = asyncio.Lock()
        return self._start_lock

    async def _launch_ssh(self) -> None:
        """Start the ssh subprocess that keeps the port-forward open.

        Selects a new free local port on each call so that re-launches
        after a crash don't collide with TIME_WAIT sockets.
        """
        self._local_port = _find_free_port()
        key_path = _expand_key_path(config.VPS_SSH_KEY)

        cmd = [
            "ssh",
            "-N",                                   # no remote command
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ServerAliveInterval=15",         # detect dead connections fast
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-i", key_path,
            "-L", f"{self._local_port}:localhost:{config.VPS_ACE_PORT}",
            f"{config.VPS_USER}@{config.VPS_HOST}",
        ]

        logger.info(
            "Opening SSH tunnel: local_port=%d -> %s:%d",
            self._local_port,
            config.VPS_HOST,
            config.VPS_ACE_PORT,
        )

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _wait_for_tunnel_ready(self, timeout: float = 10.0) -> bool:
        """Poll the /health endpoint until the tunnel is connectable.

        Returns True when the tunnel responds, False on timeout.
        """
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(f"{self.base_url}/health")
                    if resp.status_code < 500:
                        logger.info("Tunnel ready on port %d", self._local_port)
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.5)
        logger.warning("Tunnel did not become ready within %.1fs", timeout)
        return False

    async def _is_alive(self) -> bool:
        """Return True if the SSH process is running and the tunnel responds."""
        if self._process is None or self._process.returncode is not None:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.status_code < 500
        except Exception:
            return False

    async def _kill_process(self) -> None:
        """Terminate the SSH subprocess gracefully."""
        if self._process is not None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        """Return the HTTP base URL reachable through the tunnel.

        Example: 'http://127.0.0.1:54321'
        """
        if self._local_port is None:
            raise RuntimeError("Tunnel has not been started yet.")
        return f"http://127.0.0.1:{self._local_port}"

    @property
    def is_active(self) -> bool:
        """True when the tunnel process is alive (no guarantee of connectivity)."""
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Start the SSH tunnel and launch the background health monitor.

        Safe to call multiple times; subsequent calls are no-ops if the
        tunnel is already running.
        """
        async with self._lock:
            if self._running:
                return
            self._running = True
            await self._launch_ssh()
            await self._wait_for_tunnel_ready()
            self._monitor_task = asyncio.create_task(
                self._monitor_loop(), name="tunnel-monitor"
            )
            logger.info("TunnelManager started.")

    async def stop(self) -> None:
        """Stop the SSH tunnel and cancel the background monitor task."""
        self._running = False
        if self._monitor_task is not None:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        await self._kill_process()
        logger.info("TunnelManager stopped.")

    async def ensure_running(self) -> None:
        """Ensure the tunnel is up, (re-)starting it if necessary.

        This is safe to call concurrently from multiple coroutines; only
        one will actually restart the tunnel at a time because of the lock.
        """
        async with self._lock:
            if await self._is_alive():
                return
            logger.warning("Tunnel is down; attempting restart (backoff=%.1fs).", self._backoff)
            await asyncio.sleep(self._backoff)
            await self._kill_process()
            await self._launch_ssh()
            ready = await self._wait_for_tunnel_ready()
            if ready:
                self._backoff = 1.0  # reset on success
            else:
                # Increase backoff, cap at 30 s
                self._backoff = min(self._backoff * 2, 30.0)

    # ------------------------------------------------------------------
    # Background monitor
    # ------------------------------------------------------------------

    async def _monitor_loop(self) -> None:
        """Continuously verify tunnel health and reconnect when needed.

        Runs as a long-lived asyncio Task started by ``start()``.
        """
        interval = config.TUNNEL_RECONNECT_INTERVAL
        logger.debug("Tunnel monitor loop started (interval=%ds).", interval)
        while self._running:
            await asyncio.sleep(interval)
            try:
                if not await self._is_alive():
                    logger.warning("Tunnel monitor: tunnel appears dead; reconnecting.")
                    await self._kill_process()
                    await self._launch_ssh()
                    ready = await self._wait_for_tunnel_ready()
                    if ready:
                        self._backoff = 1.0
                        logger.info("Tunnel monitor: reconnect successful.")
                    else:
                        self._backoff = min(self._backoff * 2, 30.0)
                        logger.error(
                            "Tunnel monitor: reconnect failed (next backoff=%.1fs).",
                            self._backoff,
                        )
                else:
                    logger.debug("Tunnel monitor: tunnel OK on port %d.", self._local_port)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("Tunnel monitor unexpected error: %s", exc)


# Process-wide singleton convenience accessor
tunnel_manager = TunnelManager.get_instance()
