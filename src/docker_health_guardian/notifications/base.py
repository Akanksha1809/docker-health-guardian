"""Notification channel interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from docker_health_guardian.models import Incident


class NotificationChannel(ABC):
    name: str
    enabled: bool = True

    @abstractmethod
    def send(self, incident: Incident) -> tuple[bool, str]:
        """Returns (success, detail)."""


def format_incident_text(incident: Incident) -> str:
    lines = [
        f"Incident {incident.incident_id}",
        f"Container: {incident.container_name}",
        f"Severity: {incident.severity.value}",
        f"Detected: {incident.first_seen.isoformat()}",
        f"Rule: {incident.rule_id}",
    ]
    if incident.diagnosis:
        lines.append(f"Diagnosis: {incident.diagnosis.likely_cause}")
        lines.append(f"Evidence: {'; '.join(incident.diagnosis.evidence)}")
        lines.append(f"Recommended action: {incident.diagnosis.recommended_action}")
    lines.append(f"State: {incident.state.value}")
    return "\n".join(lines)
