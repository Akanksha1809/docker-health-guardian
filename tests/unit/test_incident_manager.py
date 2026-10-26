from datetime import timedelta

import pytest

from docker_health_guardian.incidents import IncidentManager
from docker_health_guardian.models import IncidentState, RuleMatch, Severity
from docker_health_guardian.persistence import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


def match(rule_id="health_unhealthy", container="app", severity=Severity.HIGH, **evidence):
    return RuleMatch(
        rule_id=rule_id,
        container_name=container,
        container_id=f"id-{container}",
        incident_type=rule_id,
        severity=severity,
        evidence=evidence,
    )


class TestProcessTriggered:
    def test_creates_new_incident_with_diagnosis(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match(health_status="unhealthy"))
        assert incident.state == IncidentState.INVESTIGATING
        assert incident.diagnosis is not None
        assert db.count_incidents() == 1

    def test_second_trigger_updates_same_incident_not_duplicate(self, db):
        mgr = IncidentManager(db)
        first = mgr.process_triggered(match())
        second = mgr.process_triggered(match())
        assert first.incident_id == second.incident_id
        assert db.count_incidents() == 1
        assert second.occurrence_count == 2

    def test_events_recorded_for_detection_and_diagnosis(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match())
        events = db.list_events(incident.incident_id)
        kinds = [e.kind for e in events]
        assert "state_change" in kinds
        assert "diagnosis" in kinds


class TestProcessCleared:
    def test_resolves_open_incident(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match())
        resolved = mgr.process_cleared("app", "health_unhealthy")
        assert resolved.incident_id == incident.incident_id
        assert resolved.state == IncidentState.RESOLVED
        assert resolved.resolved_at is not None

    def test_no_open_incident_returns_none(self, db):
        mgr = IncidentManager(db)
        assert mgr.process_cleared("app", "health_unhealthy") is None


class TestCooldown:
    def test_suppresses_new_incident_within_cooldown_window(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match())
        mgr.process_cleared("app", "health_unhealthy")

        # health_unhealthy rule has a 180s cooldown -- re-trigger immediately
        again = mgr.process_triggered(match())
        assert again.incident_id == incident.incident_id
        assert again.state == IncidentState.RESOLVED  # unchanged, not reopened
        assert db.count_incidents() == 1

    def test_creates_new_incident_after_cooldown_elapses(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match())
        resolved = mgr.process_cleared("app", "health_unhealthy")
        # simulate cooldown having elapsed by backdating resolved_at directly in storage
        resolved.resolved_at = resolved.resolved_at - timedelta(seconds=999)
        db.upsert_incident(resolved)

        new_incident = mgr.process_triggered(match())
        assert new_incident.incident_id != incident.incident_id
        assert db.count_incidents() == 2


class TestTransitions:
    def test_acknowledge_sets_state(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match())
        acked = mgr.acknowledge(incident.incident_id)
        assert acked.state == IncidentState.ACKNOWLEDGED

    def test_suppress_sets_state_with_reason_event(self, db):
        mgr = IncidentManager(db)
        incident = mgr.process_triggered(match())
        mgr.suppress(incident.incident_id, "known flaky test container")
        fetched = db.get_incident(incident.incident_id)
        assert fetched.state == IncidentState.SUPPRESSED
        events = db.list_events(incident.incident_id)
        assert any("known flaky test container" in e.detail for e in events)
