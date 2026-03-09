from __future__ import annotations

import os
from typing import Any

from ace_net_manager.settings import ManagerSettings

try:
    import docker
    from docker.errors import ImageNotFound, NotFound
except Exception:  # pragma: no cover
    docker = None
    NotFound = Exception
    ImageNotFound = Exception


class DockerService:
    def __init__(self, settings: ManagerSettings) -> None:
        if docker is None:
            raise RuntimeError("docker SDK is not installed")
        self._settings = settings
        if settings.ace_docker_base_url:
            self._client = docker.DockerClient(base_url=settings.ace_docker_base_url)
        else:
            self._client = docker.from_env()

    def ensure_network(self, name: str) -> None:
        networks = self._client.networks.list(names=[name])
        if not networks:
            self._client.networks.create(name, driver="bridge")

    def ensure_volume(self, name: str, *, labels: dict[str, str] | None = None) -> None:
        try:
            volume = self._client.volumes.get(name)
            actual_labels = volume.attrs.get("Labels") or {}
            for key, expected in (labels or {}).items():
                if str(actual_labels.get(key) or "") != str(expected):
                    raise RuntimeError(f"volume {name} failed identity check for {key}")
            return
        except NotFound:
            pass
        self._client.volumes.create(name=name, labels=labels or {})

    def pull_image(self, image_tag: str) -> None:
        try:
            self._client.images.get(image_tag)
            return
        except ImageNotFound:
            pass
        self._client.images.pull(image_tag)

    def create_container(
        self,
        *,
        name: str,
        image_tag: str,
        network_name: str,
        volume_name: str,
        labels: dict[str, str],
        environment: dict[str, str],
        internal_port: int,
        global_skills_dir: str | None = None,
        runtime_skills_mount: str | None = None,
    ) -> str:
        volumes = {volume_name: {"bind": self._settings.ace_agent_data_mount, "mode": "rw"}}
        if global_skills_dir and runtime_skills_mount and os.path.exists(str(global_skills_dir)):
            volumes[str(global_skills_dir)] = {"bind": str(runtime_skills_mount), "mode": "ro"}
        container = self._client.containers.create(
            image=image_tag,
            name=name,
            detach=True,
            network=network_name,
            labels=labels,
            environment=environment,
            volumes=volumes,
            ports={f"{internal_port}/tcp": None},
        )
        return str(container.id)

    def start_container(self, name: str) -> None:
        self._client.containers.get(name).start()

    def stop_container(self, name: str, timeout: int = 20) -> None:
        try:
            self._client.containers.get(name).stop(timeout=timeout)
        except NotFound:
            return

    def remove_container(self, name: str, force: bool = False) -> None:
        try:
            self._client.containers.get(name).remove(force=force)
        except NotFound:
            return

    def stop_and_remove_container(self, name: str, *, timeout: int = 20, force: bool = False) -> None:
        self.stop_container(name, timeout=timeout)
        self.remove_container(name, force=force)

    def rename_container(self, current_name: str, new_name: str) -> None:
        if current_name == new_name:
            return
        if self.container_exists(new_name):
            self.remove_container(new_name, force=True)
        self._client.containers.get(current_name).rename(new_name)

    def cleanup_failed_container(
        self,
        name: str,
        *,
        timeout: int = 20,
        preserve: bool = False,
        preserved_name: str | None = None,
    ) -> None:
        if preserve:
            self.stop_container(name, timeout=timeout)
            if preserved_name and self.container_exists(name):
                self.rename_container(name, preserved_name)
            return
        self.stop_and_remove_container(name, timeout=timeout)

    def get_container(self, name: str) -> Any | None:
        try:
            return self._client.containers.get(name)
        except NotFound:
            return None

    def container_exists(self, name: str) -> bool:
        return self.get_container(name) is not None

    def container_id(self, name: str) -> str | None:
        container = self.get_container(name)
        if container is None:
            return None
        return str(container.id)

    def is_running(self, name: str) -> bool:
        container = self.get_container(name)
        if container is None:
            return False
        container.reload()
        return (container.status or "").lower() == "running"

    def get_published_port(self, name: str, internal_port: int) -> int | None:
        container = self.get_container(name)
        if container is None:
            return None
        container.reload()
        ports = (container.attrs.get("NetworkSettings", {}) or {}).get("Ports", {}) or {}
        bindings = ports.get(f"{int(internal_port)}/tcp")
        if not bindings:
            return None
        host_port = bindings[0].get("HostPort")
        try:
            return int(host_port)
        except Exception:
            return None

    def assert_container_matches(
        self,
        name: str,
        *,
        image_tag: str | None,
        network_name: str,
        volume_name: str,
        labels: dict[str, str],
    ) -> None:
        container = self.get_container(name)
        if container is None:
            raise RuntimeError(f"container {name} not found")
        container.reload()
        attrs = container.attrs or {}
        config = attrs.get("Config", {}) or {}
        actual_labels = config.get("Labels", {}) or {}
        for key, expected in labels.items():
            if str(actual_labels.get(key) or "") != str(expected):
                raise RuntimeError(f"container {name} failed identity check for {key}")

        if image_tag is not None:
            actual_image = str(config.get("Image") or "")
            if actual_image != str(image_tag):
                raise RuntimeError(
                    f"container {name} is running {actual_image or 'unknown image'}, expected {image_tag}"
                )

        networks = ((attrs.get("NetworkSettings") or {}).get("Networks") or {})
        if network_name not in networks:
            raise RuntimeError(f"container {name} is not attached to network {network_name}")

        mounts = attrs.get("Mounts") or []
        volume_ok = any(
            str(mount.get("Type") or "") == "volume"
            and str(mount.get("Name") or "") == str(volume_name)
            and str(mount.get("Destination") or "") == self._settings.ace_agent_data_mount
            for mount in mounts
        )
        if not volume_ok:
            raise RuntimeError(f"container {name} is not bound to expected volume {volume_name}")
