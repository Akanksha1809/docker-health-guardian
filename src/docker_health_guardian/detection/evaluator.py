"""Stateful rolling-window health evaluation.

`HealthEvaluator` keeps a small amount of per-container state (exceeded-since
timestamps, restart event history, a bounded sample window) so that
threshold rules require *sustained* breaches rather than firing on every
transient spike, per the project's detection requirements.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from docker_health_guardian.config.models import DetectionConfig
from docker_health_guardian.models import (
    ContainerSnapshot,
    ContainerState,
    HealthStatus,
    RuleMatch,
)

from .rules import RULES

_STOPPED_STATES = {ContainerState.EXITED, ContainerState.DEAD, ContainerState.CREATED}

SYSTEM_PSEUDO_CONTAINER = "__guardian_system__"


@dataclass
class _ContainerTrack:
    cpu_exceeded_since: datetime | None = None
    memory_exceeded_since: datetime | None = None
    stopped_since: datetime | None = None
    restart_events: deque[datetime] = field(default_factory=deque)
    last_restart_count: int = 0
    consecutive_unreliable: int = 0
    last_state: ContainerState | None = None
    memory_samples: deque[int] = field(default_factory=lambda: deque(maxlen=16))


class HealthEvaluator:
    def __init__(self, config: DetectionConfig) -> None:
        self._config = config
        self._tracks: dict[str, _ContainerTrack] = {}
        self._daemon_failure_streak = 0

    def _match(
        self, rule_id: str, snapshot: ContainerSnapshot, evidence: dict
    ) -> RuleMatch:
        rule = RULES[rule_id]
        return RuleMatch(
            rule_id=rule_id,
            container_name=snapshot.name,
            container_id=snapshot.container_id,
            incident_type=rule_id,
            severity=rule.severity,
            evidence=evidence,
        )

    def evaluate(self, snapshot: ContainerSnapshot) -> tuple[list[RuleMatch], list[str]]:
        """Returns (triggered_matches, cleared_rule_ids) for one container snapshot."""
        if snapshot.monitoring_excluded:
            return [], list(RULES.keys())

        track = self._tracks.setdefault(snapshot.name, _ContainerTrack())
        now = snapshot.observed_at
        cfg = self._config
        triggered: list[RuleMatch] = []
        cleared: list[str] = []

        # -- health_unhealthy (edge: Docker already debounces its own healthcheck retries) --
        if snapshot.health_status == HealthStatus.UNHEALTHY:
            triggered.append(
                self._match(
                    "health_unhealthy",
                    snapshot,
                    {"health_status": snapshot.health_status.value},
                )
            )
        else:
            cleared.append("health_unhealthy")

        # -- unexpected_stop (edge-triggered transition) --
        # Clears once the container is confirmed running again -- the stop
        # itself stays on record in the incident's event timeline, but an
        # open incident here would otherwise never resolve (there is no
        # other sustained condition to watch), permanently absorbing every
        # later occurrence into one ever-growing incident instead of
        # letting a genuinely new failure raise a fresh one.
        if (
            track.last_state == ContainerState.RUNNING
            and snapshot.state in (ContainerState.EXITED, ContainerState.DEAD)
            and snapshot.exit_code not in (0, None)
        ):
            triggered.append(
                self._match(
                    "unexpected_stop",
                    snapshot,
                    {"exit_code": snapshot.exit_code, "previous_state": "running"},
                )
            )
        elif snapshot.state == ContainerState.RUNNING:
            cleared.append("unexpected_stop")

        # -- stopped_beyond_grace (sustained) --
        if snapshot.state in _STOPPED_STATES:
            if track.stopped_since is None:
                track.stopped_since = now
            stopped_for = (now - track.stopped_since).total_seconds()
            if stopped_for >= cfg.stopped_grace_period_seconds:
                triggered.append(
                    self._match(
                        "stopped_beyond_grace",
                        snapshot,
                        {
                            "stopped_for_seconds": round(stopped_for, 1),
                            "state": snapshot.state.value,
                        },
                    )
                )
        else:
            track.stopped_since = None
            cleared.append("stopped_beyond_grace")

        # -- restart_loop (rolling window of restart-count increments) --
        if snapshot.restart_count > track.last_restart_count:
            track.restart_events.append(now)
        track.last_restart_count = snapshot.restart_count
        while (
            track.restart_events
            and (now - track.restart_events[0]).total_seconds() > cfg.restart_window_seconds
        ):
            track.restart_events.popleft()
        if len(track.restart_events) >= cfg.restart_count_threshold:
            triggered.append(
                self._match(
                    "restart_loop",
                    snapshot,
                    {
                        "restarts_in_window": len(track.restart_events),
                        "window_seconds": cfg.restart_window_seconds,
                    },
                )
            )
        else:
            cleared.append("restart_loop")

        # -- startup_health_timeout (sustained since container start) --
        if (
            snapshot.state == ContainerState.RUNNING
            and snapshot.health_status == HealthStatus.STARTING
            and snapshot.started_at is not None
        ):
            elapsed = (now - snapshot.started_at).total_seconds()
            if elapsed >= cfg.startup_health_grace_seconds:
                triggered.append(
                    self._match(
                        "startup_health_timeout",
                        snapshot,
                        {"seconds_since_start": round(elapsed, 1)},
                    )
                )
        else:
            cleared.append("startup_health_timeout")

        # -- cpu_sustained_high --
        cpu = snapshot.stats.cpu_percent
        if cpu is not None and cpu >= cfg.cpu_threshold_percent:
            if track.cpu_exceeded_since is None:
                track.cpu_exceeded_since = now
            exceeded_for = (now - track.cpu_exceeded_since).total_seconds()
            if exceeded_for >= cfg.sustained_duration_seconds:
                triggered.append(
                    self._match(
                        "cpu_sustained_high",
                        snapshot,
                        {"cpu_percent": cpu, "exceeded_for_seconds": round(exceeded_for, 1)},
                    )
                )
        else:
            track.cpu_exceeded_since = None
            cleared.append("cpu_sustained_high")

        # -- memory_sustained_high --
        mem_pct = snapshot.stats.memory_percent
        if mem_pct is not None and mem_pct >= cfg.memory_threshold_percent:
            if track.memory_exceeded_since is None:
                track.memory_exceeded_since = now
            exceeded_for = (now - track.memory_exceeded_since).total_seconds()
            if exceeded_for >= cfg.sustained_duration_seconds:
                triggered.append(
                    self._match(
                        "memory_sustained_high",
                        snapshot,
                        {
                            "memory_percent": mem_pct,
                            "exceeded_for_seconds": round(exceeded_for, 1),
                        },
                    )
                )
        else:
            track.memory_exceeded_since = None
            cleared.append("memory_sustained_high")

        # -- memory_growth (monotonic rise across N samples) --
        if snapshot.stats.memory_usage_bytes is not None:
            track.memory_samples.append(snapshot.stats.memory_usage_bytes)
        window = list(track.memory_samples)[-cfg.memory_growth_intervals :]
        if len(window) >= cfg.memory_growth_intervals and all(
            b > a for a, b in zip(window, window[1:], strict=False)
        ):
            triggered.append(
                self._match(
                    "memory_growth",
                    snapshot,
                    {"samples_bytes": window},
                )
            )
        else:
            cleared.append("memory_growth")

        # -- metrics_unreliable --
        if not snapshot.stats.reliable and snapshot.state == ContainerState.RUNNING:
            track.consecutive_unreliable += 1
        else:
            track.consecutive_unreliable = 0
        if track.consecutive_unreliable >= cfg.collection_failure_threshold:
            triggered.append(
                self._match(
                    "metrics_unreliable",
                    snapshot,
                    {
                        "consecutive_failures": track.consecutive_unreliable,
                        "reason": snapshot.stats.unavailable_reason,
                    },
                )
            )
        else:
            cleared.append("metrics_unreliable")

        track.last_state = snapshot.state
        return triggered, cleared

    def evaluate_daemon_reachability(self, reachable: bool) -> tuple[list[RuleMatch], list[str]]:
        """Separate, container-less check for the monitoring-reliability rule."""
        if reachable:
            self._daemon_failure_streak = 0
            return [], ["docker_daemon_unreachable"]

        self._daemon_failure_streak += 1
        if self._daemon_failure_streak >= self._config.collection_failure_threshold:
            rule = RULES["docker_daemon_unreachable"]
            match = RuleMatch(
                rule_id="docker_daemon_unreachable",
                container_name=SYSTEM_PSEUDO_CONTAINER,
                container_id=SYSTEM_PSEUDO_CONTAINER,
                incident_type="docker_daemon_unreachable",
                severity=rule.severity,
                evidence={"consecutive_failures": self._daemon_failure_streak},
            )
            return [match], []
        return [], []

    def forget(self, container_name: str) -> None:
        self._tracks.pop(container_name, None)
