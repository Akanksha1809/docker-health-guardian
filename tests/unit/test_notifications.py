import pytest

from docker_health_guardian.config.models import (
    ConsoleNotificationConfig,
    EmailNotificationConfig,
    NotificationsConfig,
    WebhookNotificationConfig,
)
from docker_health_guardian.models import (
    Incident,
    NotificationOutcome,
    Severity,
)
from docker_health_guardian.notifications import ConsoleChannel, NotificationManager
from docker_health_guardian.notifications.email import EmailChannel
from docker_health_guardian.notifications.webhook import WebhookChannel
from docker_health_guardian.persistence import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


def make_incident(severity=Severity.HIGH) -> Incident:
    return Incident(
        container_name="app",
        container_id="id-app",
        rule_id="health_unhealthy",
        incident_type="health_unhealthy",
        severity=severity,
    )


class FakeSmtp:
    """Stands in for smtplib.SMTP -- no real server or credentials needed."""

    sent_messages: list = []
    fail: bool = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def send_message(self, msg):
        if self.fail:
            import smtplib

            raise smtplib.SMTPException("simulated failure")
        FakeSmtp.sent_messages.append(msg)


class FakeHttpResponse:
    def __init__(self, status_code=200, fail=False):
        self.status_code = status_code
        self._fail = fail

    def raise_for_status(self):
        if self._fail:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)


class FakeHttpClient:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append((url, json, headers))
        return FakeHttpResponse(fail=self.fail)


class TestConsoleChannel:
    def test_disabled_console_does_not_send(self):
        channel = ConsoleChannel(ConsoleNotificationConfig(enabled=False))
        assert channel.enabled is False

    def test_enabled_console_always_succeeds(self):
        channel = ConsoleChannel(ConsoleNotificationConfig(enabled=True))
        ok, _ = channel.send(make_incident())
        assert ok is True


class TestEmailChannel:
    def test_disabled_channel_does_not_attempt_send(self):
        cfg = EmailNotificationConfig(enabled=False)
        channel = EmailChannel(cfg)
        ok, detail = channel.send(make_incident())
        assert ok is False
        assert "disabled" in detail

    def test_enabled_channel_sends_via_injected_smtp(self):
        FakeSmtp.sent_messages = []
        FakeSmtp.fail = False
        cfg = EmailNotificationConfig(
            enabled=True,
            smtp_host="localhost",
            from_addr="guardian@example.test",
            to_addrs=["ops@example.test"],
        )
        channel = EmailChannel(cfg, smtp_factory=lambda: FakeSmtp())
        ok, detail = channel.send(make_incident())
        assert ok is True
        assert len(FakeSmtp.sent_messages) == 1

    def test_smtp_failure_is_reported_not_raised(self):
        FakeSmtp.fail = True
        cfg = EmailNotificationConfig(
            enabled=True,
            smtp_host="localhost",
            from_addr="guardian@example.test",
            to_addrs=["ops@example.test"],
        )
        channel = EmailChannel(cfg, smtp_factory=lambda: FakeSmtp())
        ok, detail = channel.send(make_incident())
        assert ok is False
        assert "smtp error" in detail
        FakeSmtp.fail = False


class TestWebhookChannel:
    def test_disabled_channel_does_not_send(self):
        channel = WebhookChannel(WebhookNotificationConfig(enabled=False))
        ok, _ = channel.send(make_incident())
        assert ok is False

    def test_enabled_channel_posts_payload(self):
        http = FakeHttpClient()
        cfg = WebhookNotificationConfig(enabled=True, url="http://localhost:9999/hook")
        channel = WebhookChannel(cfg, http_client=http)
        ok, detail = channel.send(make_incident())
        assert ok is True
        assert len(http.calls) == 1
        url, payload, headers = http.calls[0]
        assert payload["container_name"] == "app"

    def test_secret_adds_hmac_signature_header(self):
        http = FakeHttpClient()
        cfg = WebhookNotificationConfig(
            enabled=True, url="http://localhost:9999/hook", secret="topsecret"
        )
        channel = WebhookChannel(cfg, http_client=http)
        channel.send(make_incident())
        _, _, headers = http.calls[0]
        assert headers["X-Guardian-Signature"].startswith("sha256=")

    def test_http_failure_is_reported_not_raised(self):
        http = FakeHttpClient(fail=True)
        cfg = WebhookNotificationConfig(enabled=True, url="http://localhost:9999/hook")
        channel = WebhookChannel(cfg, http_client=http)
        ok, detail = channel.send(make_incident())
        assert ok is False


class TestNotificationManager:
    def test_min_severity_filters_low_severity_incidents(self, db):
        cfg = NotificationsConfig(min_severity=Severity.HIGH)
        channel = ConsoleChannel(ConsoleNotificationConfig(enabled=True))
        manager = NotificationManager(cfg, db, channels=[channel])
        records = manager.notify(make_incident(severity=Severity.LOW))
        assert records == []

    def test_sends_through_enabled_channel_and_persists_record(self, db):
        cfg = NotificationsConfig(min_severity=Severity.LOW)
        channel = ConsoleChannel(ConsoleNotificationConfig(enabled=True))
        manager = NotificationManager(cfg, db, channels=[channel])
        incident = make_incident()
        db.upsert_incident(incident)
        records = manager.notify(incident)
        assert len(records) == 1
        assert records[0].outcome == NotificationOutcome.SENT
        assert len(db.list_notification_records(incident_id=incident.incident_id)) == 1

    def test_cooldown_suppresses_repeat_notifications(self, db):
        cfg = NotificationsConfig(min_severity=Severity.LOW, cooldown_seconds=300)
        channel = ConsoleChannel(ConsoleNotificationConfig(enabled=True))
        manager = NotificationManager(cfg, db, channels=[channel])
        incident = make_incident()
        db.upsert_incident(incident)
        manager.notify(incident)
        records = manager.notify(incident)
        assert records[0].outcome == NotificationOutcome.SUPPRESSED

    def test_retries_with_backoff_then_marks_failed(self, db):
        class AlwaysFailChannel:
            name = "flaky"
            enabled = True
            call_count = 0

            def send(self, incident):
                AlwaysFailChannel.call_count += 1
                return False, "simulated transient error"

        cfg = NotificationsConfig(
            min_severity=Severity.LOW, max_retries=2, retry_backoff_seconds=0.001
        )
        sleeps = []
        manager = NotificationManager(
            cfg, db, channels=[AlwaysFailChannel()], sleep=lambda s: sleeps.append(s)
        )
        incident = make_incident()
        db.upsert_incident(incident)
        records = manager.notify(incident)
        assert records[0].outcome == NotificationOutcome.FAILED
        assert AlwaysFailChannel.call_count == 3  # 1 initial + 2 retries
        assert len(sleeps) == 2
        assert sleeps[1] == sleeps[0] * 2  # exponential backoff

    def test_recovers_on_retry(self, db):
        class FlakyOnceChannel:
            name = "flaky"
            enabled = True
            attempts = 0

            def send(self, incident):
                FlakyOnceChannel.attempts += 1
                if FlakyOnceChannel.attempts == 1:
                    return False, "transient"
                return True, "ok"

        cfg = NotificationsConfig(
            min_severity=Severity.LOW, max_retries=2, retry_backoff_seconds=0.001
        )
        manager = NotificationManager(
            cfg, db, channels=[FlakyOnceChannel()], sleep=lambda s: None
        )
        incident = make_incident()
        db.upsert_incident(incident)
        records = manager.notify(incident)
        assert records[0].outcome == NotificationOutcome.SENT
