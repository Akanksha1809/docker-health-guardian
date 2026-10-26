import pytest

from docker_health_guardian.models import (
    Incident,
    IncidentEvent,
    IncidentState,
    NotificationOutcome,
    NotificationRecord,
    RemediationAttempt,
    RemediationOutcome,
    Severity,
    utcnow,
)
from docker_health_guardian.persistence import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


def make_incident(**overrides) -> Incident:
    defaults = dict(
        container_name="app",
        container_id="abc123",
        rule_id="health_unhealthy",
        incident_type="health_unhealthy",
        severity=Severity.HIGH,
    )
    defaults.update(overrides)
    return Incident(**defaults)


class TestIncidentCrud:
    def test_upsert_and_get_roundtrip(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        fetched = db.get_incident(incident.incident_id)
        assert fetched is not None
        assert fetched.container_name == "app"
        assert fetched.severity == Severity.HIGH
        assert fetched.state == IncidentState.DETECTED

    def test_update_existing_incident_does_not_duplicate(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        incident.occurrence_count = 5
        incident.state = IncidentState.INVESTIGATING
        db.upsert_incident(incident)
        assert db.count_incidents() == 1
        fetched = db.get_incident(incident.incident_id)
        assert fetched.occurrence_count == 5
        assert fetched.state == IncidentState.INVESTIGATING

    def test_find_open_incident_excludes_resolved(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        assert db.find_open_incident("app", "health_unhealthy") is not None
        incident.state = IncidentState.RESOLVED
        incident.resolved_at = utcnow()
        db.upsert_incident(incident)
        assert db.find_open_incident("app", "health_unhealthy") is None
        assert db.find_last_resolved("app", "health_unhealthy") is not None

    def test_list_incidents_filters_by_state_and_severity(self, db):
        db.upsert_incident(make_incident(severity=Severity.LOW))
        db.upsert_incident(make_incident(container_name="app2", severity=Severity.CRITICAL))
        assert len(db.list_incidents(severity="low")) == 1
        assert len(db.list_incidents(severity="critical")) == 1
        assert len(db.list_incidents(container_name="app2")) == 1

    def test_diagnosis_roundtrips_through_json(self, db):
        from docker_health_guardian.models import Confidence, Diagnosis

        incident = make_incident()
        incident.diagnosis = Diagnosis(
            likely_cause="test cause",
            confidence=Confidence.HIGH,
            evidence=["ev1"],
            recommended_action="do something",
            safe_to_automate=True,
        )
        db.upsert_incident(incident)
        fetched = db.get_incident(incident.incident_id)
        assert fetched.diagnosis.likely_cause == "test cause"
        assert fetched.diagnosis.confidence == Confidence.HIGH


class TestIncidentEvents:
    def test_events_persist_in_order(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        db.add_event(IncidentEvent(incident_id=incident.incident_id, kind="a", detail="first"))
        db.add_event(IncidentEvent(incident_id=incident.incident_id, kind="b", detail="second"))
        events = db.list_events(incident.incident_id)
        assert [e.detail for e in events] == ["first", "second"]


class TestRemediationAttempts:
    def test_upsert_then_update_outcome(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        attempt = RemediationAttempt(
            incident_id=incident.incident_id,
            container_name="app",
            action="restart",
            dry_run=False,
            reason="health_unhealthy",
            outcome=RemediationOutcome.SKIPPED,
        )
        db.upsert_remediation_attempt(attempt)
        attempt.outcome = RemediationOutcome.SUCCESS
        attempt.recovery_verified = True
        db.upsert_remediation_attempt(attempt)

        fetched = db.list_remediation_attempts(incident_id=incident.incident_id)
        assert len(fetched) == 1
        assert fetched[0].outcome == RemediationOutcome.SUCCESS
        assert fetched[0].recovery_verified is True

    def test_count_attempts_for_incident_excludes_dry_run_by_default(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        for _ in range(2):
            db.upsert_remediation_attempt(
                RemediationAttempt(
                    incident_id=incident.incident_id,
                    container_name="app",
                    action="restart",
                    dry_run=False,
                    reason="r",
                    outcome=RemediationOutcome.SUCCESS,
                )
            )
        db.upsert_remediation_attempt(
            RemediationAttempt(
                incident_id=incident.incident_id,
                container_name="app",
                action="restart",
                dry_run=True,
                reason="r",
                outcome=RemediationOutcome.SUCCESS,
            )
        )
        assert db.count_attempts_for_incident(incident.incident_id, dry_run=False) == 2
        assert db.count_attempts_for_incident(incident.incident_id, dry_run=True) == 1


class TestNotifications:
    def test_add_and_list_notification_records(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        db.add_notification_record(
            NotificationRecord(
                incident_id=incident.incident_id,
                channel="console",
                outcome=NotificationOutcome.SENT,
            )
        )
        records = db.list_notification_records(incident_id=incident.incident_id)
        assert len(records) == 1
        assert records[0].outcome == NotificationOutcome.SENT


class TestRetention:
    def test_cleanup_removes_only_old_resolved_incidents(self, db):
        from datetime import timedelta

        open_incident = make_incident(container_name="open-app")
        db.upsert_incident(open_incident)

        old_resolved = make_incident(container_name="old-app")
        old_resolved.state = IncidentState.RESOLVED
        old_resolved.resolved_at = utcnow() - timedelta(days=100)
        db.upsert_incident(old_resolved)

        recent_resolved = make_incident(container_name="recent-app")
        recent_resolved.state = IncidentState.RESOLVED
        recent_resolved.resolved_at = utcnow() - timedelta(days=1)
        db.upsert_incident(recent_resolved)

        removed = db.cleanup_retention(retention_days=30)
        assert removed == 1
        assert db.get_incident(old_resolved.incident_id) is None
        assert db.get_incident(open_incident.incident_id) is not None
        assert db.get_incident(recent_resolved.incident_id) is not None
