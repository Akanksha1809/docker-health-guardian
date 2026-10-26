"""Orchestrates notification delivery across channels: severity filtering,
per-incident-per-channel cooldown (storm prevention), and bounded
exponential-backoff retry on transient failures.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from docker_health_guardian.config.models import NotificationsConfig
from docker_health_guardian.models import (
    Incident,
    NotificationOutcome,
    NotificationRecord,
    Severity,
    utcnow,
)
from docker_health_guardian.persistence import Database

from .base import NotificationChannel
from .console import ConsoleChannel

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {
    Severity.LOW: 0,
    Severity.MEDIUM: 1,
    Severity.HIGH: 2,
    Severity.CRITICAL: 3,
}


class NotificationManager:
    def __init__(
        self,
        cfg: NotificationsConfig,
        db: Database,
        channels: list[NotificationChannel] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = cfg
        self._db = db
        self._sleep = sleep
        self._channels = channels if channels is not None else self._build_default_channels()

    def _build_default_channels(self) -> list[NotificationChannel]:
        channels: list[NotificationChannel] = [ConsoleChannel(self._cfg.console)]
        if self._cfg.email.enabled:
            from .email import EmailChannel

            channels.append(EmailChannel(self._cfg.email))
        if self._cfg.webhook.enabled:
            from .webhook import WebhookChannel

            channels.append(WebhookChannel(self._cfg.webhook))
        return channels

    def notify(self, incident: Incident) -> list[NotificationRecord]:
        if _SEVERITY_RANK[incident.severity] < _SEVERITY_RANK[self._cfg.min_severity]:
            return []

        records: list[NotificationRecord] = []
        for channel in self._channels:
            if not channel.enabled:
                continue
            if self._in_cooldown(incident.incident_id, channel.name):
                logger.debug(
                    "suppressing %s notification for %s: cooldown active",
                    channel.name,
                    incident.incident_id,
                )
                record = NotificationRecord(
                    incident_id=incident.incident_id,
                    channel=channel.name,
                    outcome=NotificationOutcome.SUPPRESSED,
                    detail="cooldown active",
                )
            else:
                outcome, detail = self._send_with_retry(channel, incident)
                record = NotificationRecord(
                    incident_id=incident.incident_id,
                    channel=channel.name,
                    outcome=outcome,
                    detail=detail,
                )
            self._db.add_notification_record(record)
            records.append(record)
        return records

    def _in_cooldown(self, incident_id: str, channel_name: str) -> bool:
        recent = self._db.list_notification_records(incident_id=incident_id, limit=10)
        for record in recent:
            if record.channel != channel_name:
                continue
            if record.outcome != NotificationOutcome.SENT:
                continue
            elapsed = (utcnow() - record.attempted_at).total_seconds()
            if elapsed < self._cfg.cooldown_seconds:
                return True
        return False

    def _send_with_retry(
        self, channel: NotificationChannel, incident: Incident
    ) -> tuple[NotificationOutcome, str]:
        backoff = self._cfg.retry_backoff_seconds
        last_detail = ""
        for attempt in range(self._cfg.max_retries + 1):
            ok, detail = channel.send(incident)
            if ok:
                return NotificationOutcome.SENT, detail
            last_detail = detail
            if attempt < self._cfg.max_retries:
                self._sleep(backoff)
                backoff *= 2
        logger.warning(
            "notification via %s failed after %d attempt(s): %s",
            channel.name,
            self._cfg.max_retries + 1,
            last_detail,
        )
        return NotificationOutcome.FAILED, last_detail
