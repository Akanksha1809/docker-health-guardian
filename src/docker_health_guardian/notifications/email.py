"""SMTP email notifications.

`smtp_factory` is injectable so tests never need a real mail server or real
credentials -- see tests/unit/test_notifications.py for a fake SMTP client.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage

from docker_health_guardian.config.models import EmailNotificationConfig
from docker_health_guardian.models import Incident

from .base import NotificationChannel, format_incident_text


class EmailChannel(NotificationChannel):
    name = "email"

    def __init__(
        self,
        cfg: EmailNotificationConfig,
        smtp_factory: Callable[[], smtplib.SMTP] | None = None,
    ) -> None:
        self._cfg = cfg
        self.enabled = cfg.enabled
        self._smtp_factory = smtp_factory or self._default_factory

    def _default_factory(self) -> smtplib.SMTP:
        return smtplib.SMTP(self._cfg.smtp_host, self._cfg.smtp_port, timeout=10)

    def _build_message(self, incident: Incident) -> EmailMessage:
        msg = EmailMessage()
        msg["Subject"] = (
            f"[Guardian] {incident.severity.value.upper()} incident on "
            f"{incident.container_name}"
        )
        msg["From"] = self._cfg.from_addr
        msg["To"] = ", ".join(self._cfg.to_addrs)
        msg.set_content(format_incident_text(incident))
        return msg

    def send(self, incident: Incident) -> tuple[bool, str]:
        if not self.enabled:
            return False, "email channel disabled"
        try:
            message = self._build_message(incident)
            with self._smtp_factory() as smtp:
                if self._cfg.use_tls:
                    smtp.starttls()
                if self._cfg.smtp_user:
                    smtp.login(self._cfg.smtp_user, self._cfg.smtp_password)
                smtp.send_message(message)
            return True, f"sent to {len(self._cfg.to_addrs)} recipient(s)"
        except (smtplib.SMTPException, OSError) as exc:
            return False, f"smtp error: {exc}"
