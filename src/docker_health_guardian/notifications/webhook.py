"""Generic webhook notifications over HTTPS, with optional HMAC-SHA256 signing.

`http_client` is injectable; tests point it at a local httpx test transport
or a fake object rather than any real network endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from docker_health_guardian.config.models import WebhookNotificationConfig
from docker_health_guardian.models import Incident

from .base import NotificationChannel


class WebhookChannel(NotificationChannel):
    name = "webhook"

    def __init__(
        self, cfg: WebhookNotificationConfig, http_client: httpx.Client | None = None
    ) -> None:
        self._cfg = cfg
        self.enabled = cfg.enabled
        self._client = http_client or httpx.Client(timeout=cfg.timeout_seconds)

    def _payload(self, incident: Incident) -> dict:
        return {
            "incident_id": incident.incident_id,
            "container_name": incident.container_name,
            "severity": incident.severity.value,
            "state": incident.state.value,
            "rule_id": incident.rule_id,
            "detected_at": incident.first_seen.isoformat(),
            "diagnosis": (
                incident.diagnosis.model_dump(mode="json") if incident.diagnosis else None
            ),
        }

    def send(self, incident: Incident) -> tuple[bool, str]:
        if not self.enabled:
            return False, "webhook channel disabled"
        payload = self._payload(incident)
        headers = {"Content-Type": "application/json"}
        if self._cfg.secret:
            body = json.dumps(payload, sort_keys=True).encode()
            signature = hmac.new(self._cfg.secret.encode(), body, hashlib.sha256).hexdigest()
            headers["X-Guardian-Signature"] = f"sha256={signature}"
        try:
            response = self._client.post(self._cfg.url, json=payload, headers=headers)
            response.raise_for_status()
            return True, f"status={response.status_code}"
        except httpx.HTTPError as exc:
            return False, f"webhook error: {exc}"
