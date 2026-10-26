"""Incident lifecycle management: dedup, cooldown-gated creation, and resolution.

Lifecycle: DETECTED -> INVESTIGATING -> ACTION_APPROVED -> REMEDIATING ->
VERIFYING -> RESOLVED, with ACKNOWLEDGED/SUPPRESSED/ESCALATED/
REMEDIATION_FAILED/RECOVERY_UNVERIFIED as alternative/exceptional states.
Not every incident passes through every state -- a pure monitoring-reliability
incident, for instance, goes DETECTED -> INVESTIGATING -> RESOLVED without
ever touching remediation states.
"""

from __future__ import annotations

import logging

from docker_health_guardian.detection.rules import get_rule
from docker_health_guardian.diagnosis import diagnose
from docker_health_guardian.models import (
    Incident,
    IncidentEvent,
    IncidentState,
    RuleMatch,
    utcnow,
)
from docker_health_guardian.persistence import Database

logger = logging.getLogger(__name__)


class IncidentManager:
    def __init__(self, db: Database) -> None:
        self._db = db

    def process_triggered(self, match: RuleMatch) -> Incident:
        """Either updates an existing open incident (dedup) or creates a new
        one, subject to the rule's cooldown since it was last resolved."""
        existing = self._db.find_open_incident(match.container_name, match.rule_id)
        if existing:
            existing.last_seen = utcnow()
            existing.occurrence_count += 1
            existing.observed_metrics = match.evidence
            self._db.upsert_incident(existing)
            self._db.add_event(
                IncidentEvent(
                    incident_id=existing.incident_id,
                    kind="observation",
                    detail=f"Rule '{match.rule_id}' re-observed (occurrence "
                    f"#{existing.occurrence_count})",
                    data=match.evidence,
                )
            )
            return existing

        rule = get_rule(match.rule_id)
        last_resolved = self._db.find_last_resolved(match.container_name, match.rule_id)
        if last_resolved and last_resolved.resolved_at:
            cooled_down_for = (utcnow() - last_resolved.resolved_at).total_seconds()
            if cooled_down_for < rule.cooldown_seconds:
                logger.info(
                    "suppressing new incident for %s/%s: still within %ss cooldown "
                    "(resolved %ss ago)",
                    match.container_name,
                    match.rule_id,
                    rule.cooldown_seconds,
                    round(cooled_down_for, 1),
                )
                return last_resolved

        incident = Incident(
            container_name=match.container_name,
            container_id=match.container_id,
            rule_id=match.rule_id,
            incident_type=match.incident_type,
            severity=match.severity,
            state=IncidentState.DETECTED,
            observed_metrics=match.evidence,
        )
        incident.diagnosis = diagnose(match)
        incident.state = IncidentState.INVESTIGATING
        self._db.upsert_incident(incident)
        self._db.add_event(
            IncidentEvent(
                incident_id=incident.incident_id,
                kind="state_change",
                detail="Incident detected",
                data=match.evidence,
            )
        )
        self._db.add_event(
            IncidentEvent(
                incident_id=incident.incident_id,
                kind="diagnosis",
                detail=incident.diagnosis.likely_cause,
                data=incident.diagnosis.model_dump(mode="json"),
            )
        )
        logger.info(
            "new incident %s: %s on %s (%s)",
            incident.incident_id,
            match.rule_id,
            match.container_name,
            match.severity.value,
        )
        return incident

    def process_cleared(self, container_name: str, rule_id: str) -> Incident | None:
        """Resolves an open incident whose recovery condition has been met."""
        existing = self._db.find_open_incident(container_name, rule_id)
        if not existing:
            return None
        existing.state = IncidentState.RESOLVED
        existing.resolved_at = utcnow()
        existing.last_seen = existing.resolved_at
        self._db.upsert_incident(existing)
        self._db.add_event(
            IncidentEvent(
                incident_id=existing.incident_id,
                kind="state_change",
                detail="Incident resolved: recovery condition met",
            )
        )
        logger.info("resolved incident %s (%s/%s)", existing.incident_id, container_name, rule_id)
        return existing

    def transition(self, incident: Incident, new_state: IncidentState, detail: str) -> Incident:
        incident.state = new_state
        incident.last_seen = utcnow()
        if new_state == IncidentState.RESOLVED and incident.resolved_at is None:
            incident.resolved_at = incident.last_seen
        self._db.upsert_incident(incident)
        self._db.add_event(
            IncidentEvent(incident_id=incident.incident_id, kind="state_change", detail=detail)
        )
        return incident

    def acknowledge(self, incident_id: str) -> Incident | None:
        incident = self._db.get_incident(incident_id)
        if not incident:
            return None
        return self.transition(incident, IncidentState.ACKNOWLEDGED, "Acknowledged by operator")

    def suppress(self, incident_id: str, reason: str) -> Incident | None:
        incident = self._db.get_incident(incident_id)
        if not incident:
            return None
        return self.transition(incident, IncidentState.SUPPRESSED, f"Suppressed: {reason}")
