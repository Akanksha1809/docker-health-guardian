"""Wraps the Docker SDK to produce ContainerSnapshot objects.

All Docker SDK/API errors are caught here and turned into logged warnings plus
graceful fallbacks (empty results, `monitoring_excluded`/unreliable stats)
rather than exceptions that would crash a monitoring cycle.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

import docker
from docker.errors import APIError, DockerException, NotFound

from docker_health_guardian.config.models import DockerConfig
from docker_health_guardian.models import ContainerSnapshot, ContainerState, HealthStatus

from .stats import build_resource_stats

logger = logging.getLogger(__name__)

_EXCLUSION_LABEL = "guardian.monitoring"
_EXCLUSION_LABEL_VALUE = "disabled"

_STATE_MAP = {
    "running": ContainerState.RUNNING,
    "exited": ContainerState.EXITED,
    "restarting": ContainerState.RESTARTING,
    "paused": ContainerState.PAUSED,
    "dead": ContainerState.DEAD,
    "created": ContainerState.CREATED,
    "removing": ContainerState.REMOVING,
}

_HEALTH_MAP = {
    "healthy": HealthStatus.HEALTHY,
    "unhealthy": HealthStatus.UNHEALTHY,
    "starting": HealthStatus.STARTING,
}

_NEVER_STARTED = "0001-01-01T00:00:00Z"


class DockerUnavailableError(Exception):
    """Raised when the Docker daemon cannot be reached at all."""


class DockerCollector:
    def __init__(self, config: DockerConfig, client: docker.DockerClient | None = None) -> None:
        self._config = config
        if client is not None:
            self._client = client
        elif config.socket_url:
            self._client = docker.DockerClient(
                base_url=config.socket_url, timeout=config.api_timeout_seconds
            )
        else:
            self._client = docker.from_env(timeout=config.api_timeout_seconds)
        self._host_total_memory_bytes: int | None = None

    def ping(self) -> bool:
        try:
            return bool(self._client.ping())
        except DockerException as exc:
            logger.warning("docker daemon ping failed: %s", exc)
            return False

    def _host_memory(self) -> int | None:
        if self._host_total_memory_bytes is not None:
            return self._host_total_memory_bytes
        try:
            info = self._client.info()
            self._host_total_memory_bytes = int(info.get("MemTotal") or 0) or None
        except (DockerException, ValueError) as exc:
            logger.warning("could not read docker host memory info: %s", exc)
        return self._host_total_memory_bytes

    def _is_excluded(self, name: str, labels: dict[str, str]) -> bool:
        if name in self._config.excluded_containers:
            return True
        if labels.get(_EXCLUSION_LABEL) == _EXCLUSION_LABEL_VALUE:
            return True
        monitored = self._config.monitored_containers
        if monitored and name not in monitored:
            return True
        return False

    def list_container_snapshots(self) -> list[ContainerSnapshot]:
        try:
            containers = self._client.containers.list(all=True)
        except DockerException as exc:
            raise DockerUnavailableError(str(exc)) from exc

        snapshots: list[ContainerSnapshot] = []
        for container in containers:
            try:
                snapshots.append(self._snapshot_one(container))
            except (APIError, NotFound) as exc:
                logger.warning("failed to inspect container %s: %s", container.name, exc)
        return snapshots

    def _snapshot_one(self, container: Any) -> ContainerSnapshot:
        attrs = container.attrs
        state_info = attrs.get("State", {}) or {}
        config_info = attrs.get("Config", {}) or {}
        labels = config_info.get("Labels") or {}
        name = container.name
        excluded = self._is_excluded(name, labels)

        raw_state = (state_info.get("Status") or "unknown").lower()
        state = _STATE_MAP.get(raw_state, ContainerState.UNKNOWN)

        health_block = state_info.get("Health")
        if health_block:
            health_status = _HEALTH_MAP.get(
                (health_block.get("Status") or "").lower(), HealthStatus.NONE
            )
        else:
            health_status = HealthStatus.NONE

        started_at_raw = state_info.get("StartedAt")
        started_at: datetime | None = None
        if started_at_raw and started_at_raw != _NEVER_STARTED:
            try:
                started_at = datetime.fromisoformat(started_at_raw.replace("Z", "+00:00"))
            except ValueError:
                started_at = None

        stats = build_resource_stats(None, None)
        if not excluded and state == ContainerState.RUNNING:
            raw_stats = self._fetch_raw_stats(container)
            stats = build_resource_stats(raw_stats, self._host_memory())

        return ContainerSnapshot(
            container_id=container.id,
            name=name,
            image=config_info.get("Image", "unknown"),
            state=state,
            health_status=health_status,
            started_at=started_at,
            exit_code=state_info.get("ExitCode"),
            restart_count=attrs.get("RestartCount", 0) or 0,
            labels=labels,
            stats=stats,
            monitoring_excluded=excluded,
        )

    def _fetch_raw_stats(self, container: Any) -> dict | None:
        try:
            return container.stats(stream=False)
        except (APIError, NotFound) as exc:
            logger.warning("failed to fetch stats for %s: %s", container.name, exc)
            return None

    def get_log_excerpt(self, container_name: str, tail: int | None = None) -> str:
        tail = tail or self._config.log_tail_lines
        try:
            container = self._client.containers.get(container_name)
            raw = container.logs(tail=tail, timestamps=True)
            return raw.decode("utf-8", errors="replace")
        except (APIError, NotFound, DockerException) as exc:
            logger.warning("failed to fetch logs for %s: %s", container_name, exc)
            return ""

    def get_recent_events(self, since_seconds: int = 300) -> list[dict]:
        """Returns events in the bounded window [now - since_seconds, now].

        Both `since` and `until` are passed so the underlying call returns a
        finite, already-materialized window instead of a live-streaming
        generator that would block waiting for future events.
        """
        now = time.time()
        try:
            events = self._client.events(
                since=now - since_seconds, until=now, decode=True
            )
            return list(events)[:200]
        except (APIError, DockerException) as exc:
            logger.warning("failed to fetch docker events: %s", exc)
            return []

    def restart_container(self, container_name: str, timeout_seconds: int = 10) -> None:
        container = self._client.containers.get(container_name)
        container.restart(timeout=timeout_seconds)

    def get_single_snapshot(self, container_name: str) -> ContainerSnapshot | None:
        try:
            container = self._client.containers.get(container_name)
        except (NotFound, DockerException):
            return None
        return self._snapshot_one(container)
