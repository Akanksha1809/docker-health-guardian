"""Core domain types shared across collector, detection, incidents, diagnosis,
remediation, persistence, api, and cli modules.

Kept dependency-free (stdlib + pydantic only) so every other module can import
from here without creating cycles.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class ContainerState(StrEnum):
    RUNNING = "running"
    EXITED = "exited"
    RESTARTING = "restarting"
    PAUSED = "paused"
    DEAD = "dead"
    CREATED = "created"
    REMOVING = "removing"
    UNKNOWN = "unknown"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    NONE = "none"  # container has no configured HEALTHCHECK


class ResourceStats(BaseModel):
    """A single resource sample. `None` means the metric was unavailable, not zero."""

    cpu_percent: float | None = None
    memory_usage_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_percent: float | None = None
    network_rx_bytes: int | None = None
    network_tx_bytes: int | None = None
    block_read_bytes: int | None = None
    block_write_bytes: int | None = None
    reliable: bool = True
    unavailable_reason: str | None = None


class ContainerSnapshot(BaseModel):
    """One point-in-time observation of a single container."""

    container_id: str
    name: str
    image: str
    state: ContainerState
    health_status: HealthStatus
    started_at: datetime | None = None
    exit_code: int | None = None
    restart_count: int = 0
    labels: dict[str, str] = Field(default_factory=dict)
    stats: ResourceStats = Field(default_factory=ResourceStats)
    observed_at: datetime = Field(default_factory=utcnow)
    monitoring_excluded: bool = False


class Severity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class RuleMatch(BaseModel):
    """One rule firing for one container during one evaluation cycle."""

    rule_id: str
    container_name: str
    container_id: str
    incident_type: str
    severity: Severity
    evidence: dict[str, Any] = Field(default_factory=dict)


class IncidentState(StrEnum):
    DETECTED = "detected"
    INVESTIGATING = "investigating"
    ACTION_APPROVED = "action_approved"
    REMEDIATING = "remediating"
    VERIFYING = "verifying"
    RESOLVED = "resolved"
    ACKNOWLEDGED = "acknowledged"
    SUPPRESSED = "suppressed"
    ESCALATED = "escalated"
    REMEDIATION_FAILED = "remediation_failed"
    RECOVERY_UNVERIFIED = "recovery_unverified"


class Confidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Diagnosis(BaseModel):
    likely_cause: str
    confidence: Confidence
    evidence: list[str] = Field(default_factory=list)
    alternative_explanations: list[str] = Field(default_factory=list)
    recommended_action: str
    safe_to_automate: bool


class IncidentEvent(BaseModel):
    """One entry in an incident's audit trail."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    kind: str  # e.g. "state_change", "observation", "action", "verification", "notification"
    detail: str
    data: dict[str, Any] = Field(default_factory=dict)


class RemediationOutcome(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DENIED = "denied"


class RemediationAttempt(BaseModel):
    attempt_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    container_name: str
    action: str
    dry_run: bool
    reason: str
    outcome: RemediationOutcome
    detail: str = ""
    started_at: datetime = Field(default_factory=utcnow)
    finished_at: datetime | None = None
    recovery_verified: bool | None = None


class Incident(BaseModel):
    incident_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    container_name: str
    container_id: str
    rule_id: str
    incident_type: str
    severity: Severity
    state: IncidentState = IncidentState.DETECTED
    first_seen: datetime = Field(default_factory=utcnow)
    last_seen: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    observed_metrics: dict[str, Any] = Field(default_factory=dict)
    diagnosis: Diagnosis | None = None
    notified: bool = False
    occurrence_count: int = 1

    def is_open(self) -> bool:
        return self.state not in (
            IncidentState.RESOLVED,
            IncidentState.SUPPRESSED,
        )


class NotificationOutcome(StrEnum):
    SENT = "sent"
    FAILED = "failed"
    SUPPRESSED = "suppressed"


class NotificationRecord(BaseModel):
    record_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident_id: str
    channel: str
    outcome: NotificationOutcome
    detail: str = ""
    attempted_at: datetime = Field(default_factory=utcnow)
