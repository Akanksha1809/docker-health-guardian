"""Prometheus metrics.

All label values below are drawn from small, fixed, code-defined sets
(container state, health status, severity, rule id, outcome) -- never from
container IDs, incident IDs, or other unbounded identifiers, per the
project's metrics requirements.

Gauges that reflect "current state" (monitored container counts, open
incidents) are kept fresh two ways: the scheduler calls the `record_*`/`set_*`
setters once per monitoring cycle as it observes containers, and the
incident-related gauges are additionally recomputed directly from
persistence on every `/metrics` scrape via `sync_metrics_from_db`, so they
are accurate even if no scheduler has run yet in this process.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

from docker_health_guardian.models import IncidentState, Severity
from docker_health_guardian.persistence import Database

MONITORED_CONTAINERS = Gauge(
    "guardian_monitored_containers", "Number of containers currently monitored"
)
CONTAINERS_BY_STATE = Gauge(
    "guardian_containers_by_state", "Monitored containers by observed state", ["state"]
)
OPEN_INCIDENTS = Gauge("guardian_open_incidents", "Number of currently open incidents")
INCIDENTS_BY_SEVERITY = Gauge(
    "guardian_incidents_by_severity", "Open incidents by severity", ["severity"]
)
REMEDIATION_ATTEMPTS = Counter(
    "guardian_remediation_attempts_total", "Remediation attempts by outcome", ["outcome"]
)
RECOVERY_VERIFICATIONS = Counter(
    "guardian_recovery_verifications_total", "Recovery verification results", ["result"]
)
COLLECTION_ERRORS = Counter(
    "guardian_collection_errors_total", "Docker collection errors encountered"
)
NOTIFICATION_FAILURES = Counter(
    "guardian_notification_failures_total", "Notification delivery failures by channel", ["channel"]
)
CYCLE_DURATION = Histogram(
    "guardian_cycle_duration_seconds", "Duration of one monitoring cycle"
)

_OPEN_STATES = [
    s for s in IncidentState if s not in (IncidentState.RESOLVED, IncidentState.SUPPRESSED)
]


def set_monitored_containers(count: int) -> None:
    MONITORED_CONTAINERS.set(count)


def set_containers_by_state(counts: dict[str, int]) -> None:
    for state in counts:
        CONTAINERS_BY_STATE.labels(state=state).set(counts[state])


def record_collection_error() -> None:
    COLLECTION_ERRORS.inc()


def record_remediation_attempt(outcome: str) -> None:
    REMEDIATION_ATTEMPTS.labels(outcome=outcome).inc()


def record_recovery_verification(success: bool) -> None:
    RECOVERY_VERIFICATIONS.labels(result="success" if success else "failed").inc()


def record_notification_failure(channel: str) -> None:
    NOTIFICATION_FAILURES.labels(channel=channel).inc()


def observe_cycle_duration(seconds: float) -> None:
    CYCLE_DURATION.observe(seconds)


def sync_metrics_from_db(db: Database) -> None:
    OPEN_INCIDENTS.set(sum(db.count_incidents(state=s.value) for s in _OPEN_STATES))
    for severity in Severity:
        count = sum(
            db.count_incidents(state=s.value, severity=severity.value) for s in _OPEN_STATES
        )
        INCIDENTS_BY_SEVERITY.labels(severity=severity.value).set(count)
