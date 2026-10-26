from __future__ import annotations

import logging

from docker_health_guardian.config.models import ConsoleNotificationConfig
from docker_health_guardian.models import Incident

from .base import NotificationChannel, format_incident_text

logger = logging.getLogger("guardian.notifications.console")


class ConsoleChannel(NotificationChannel):
    name = "console"

    def __init__(self, cfg: ConsoleNotificationConfig) -> None:
        self.enabled = cfg.enabled

    def send(self, incident: Incident) -> tuple[bool, str]:
        logger.warning("INCIDENT NOTIFICATION\n%s", format_incident_text(incident))
        return True, "printed to console"
