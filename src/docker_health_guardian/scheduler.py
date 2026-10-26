"""The monitoring loop: one collection cycle at a time, never overlapping.

Wires together collector -> evaluator -> incident manager -> remediation
engine -> notification manager -> metrics, once per `polling_interval_seconds`.
Transient Docker API errors are caught per-cycle and recorded as metrics
rather than crashing the service; only `stop()` (e.g. SIGINT/SIGTERM) ends
the loop.
"""

from __future__ import annotations

import logging
import signal
import time
from collections import Counter as _Counter
from types import FrameType

from docker_health_guardian import metrics
from docker_health_guardian.collector import DockerCollector, DockerUnavailableError
from docker_health_guardian.config.models import GuardianAppConfig
from docker_health_guardian.detection import HealthEvaluator
from docker_health_guardian.detection.evaluator import SYSTEM_PSEUDO_CONTAINER
from docker_health_guardian.incidents import IncidentManager
from docker_health_guardian.models import IncidentState
from docker_health_guardian.notifications import NotificationManager
from docker_health_guardian.persistence import Database
from docker_health_guardian.remediation import RemediationEngine

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        config: GuardianAppConfig,
        collector: DockerCollector,
        evaluator: HealthEvaluator,
        incident_manager: IncidentManager,
        remediation_engine: RemediationEngine,
        notification_manager: NotificationManager,
    ) -> None:
        self._config = config
        self._collector = collector
        self._evaluator = evaluator
        self._incident_manager = incident_manager
        self._remediation_engine = remediation_engine
        self._notification_manager = notification_manager
        self._running = False

    def run_once(self) -> None:
        start = time.monotonic()
        reachable = self._collector.ping()
        triggered, cleared = self._evaluator.evaluate_daemon_reachability(reachable)
        for match in triggered:
            incident = self._incident_manager.process_triggered(match)
            self._notification_manager.notify(incident)
        for rule_id in cleared:
            self._incident_manager.process_cleared(SYSTEM_PSEUDO_CONTAINER, rule_id)

        if not reachable:
            metrics.record_collection_error()
            metrics.observe_cycle_duration(time.monotonic() - start)
            logger.warning("docker daemon unreachable this cycle; skipping container collection")
            return

        try:
            snapshots = self._collector.list_container_snapshots()
        except DockerUnavailableError as exc:
            metrics.record_collection_error()
            metrics.observe_cycle_duration(time.monotonic() - start)
            logger.warning("collection failed this cycle: %s", exc)
            return

        active = [s for s in snapshots if not s.monitoring_excluded]
        metrics.set_monitored_containers(len(active))
        metrics.set_containers_by_state(dict(_Counter(s.state.value for s in active)))

        for snapshot in snapshots:
            triggered, cleared = self._evaluator.evaluate(snapshot)
            for match in triggered:
                self._handle_triggered(match)
            for rule_id in cleared:
                self._incident_manager.process_cleared(snapshot.name, rule_id)

        duration = time.monotonic() - start
        metrics.observe_cycle_duration(duration)
        if duration > self._config.guardian.cycle_timeout_seconds:
            logger.warning(
                "monitoring cycle took %.1fs, exceeding cycle_timeout_seconds=%.1fs",
                duration,
                self._config.guardian.cycle_timeout_seconds,
            )

    def _handle_triggered(self, match) -> None:
        incident = self._incident_manager.process_triggered(match)
        if incident.state == IncidentState.RESOLVED:
            return  # suppressed re-raise during cooldown; nothing new to act on
        self._notification_manager.notify(incident)
        attempt = self._remediation_engine.attempt_remediation(
            incident, self._config.remediation, self._collector, self._incident_manager
        )
        metrics.record_remediation_attempt(attempt.outcome.value)
        if attempt.recovery_verified is not None:
            metrics.record_recovery_verification(attempt.recovery_verified)

    def run_forever(self) -> None:
        self._running = True

        def _handle_signal(signum: int, frame: FrameType | None) -> None:
            logger.info("received signal %s, shutting down after this cycle", signum)
            self._running = False

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

        logger.info(
            "guardian started: mode=%s remediation.enabled=%s dry_run=%s interval=%ss",
            self._config.guardian.mode,
            self._config.remediation.enabled,
            self._config.remediation.dry_run,
            self._config.guardian.polling_interval_seconds,
        )
        while self._running:
            cycle_start = time.monotonic()
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 -- the service must survive one bad cycle
                logger.exception("unhandled error during monitoring cycle")
                metrics.record_collection_error()
            elapsed = time.monotonic() - cycle_start
            remaining = max(self._config.guardian.polling_interval_seconds - elapsed, 0)
            if self._running and remaining > 0:
                time.sleep(remaining)
        logger.info("guardian stopped")

    def stop(self) -> None:
        self._running = False


def build_scheduler(config: GuardianAppConfig) -> Scheduler:
    db = Database(config.persistence.db_path)
    db.initialize()
    collector = DockerCollector(config.docker)
    evaluator = HealthEvaluator(config.detection)
    incident_manager = IncidentManager(db)
    remediation_engine = RemediationEngine(db)
    notification_manager = NotificationManager(config.notifications, db)
    return Scheduler(
        config, collector, evaluator, incident_manager, remediation_engine, notification_manager
    )
