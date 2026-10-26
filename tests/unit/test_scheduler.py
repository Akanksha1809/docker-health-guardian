import pytest

from docker_health_guardian.collector import DockerUnavailableError
from docker_health_guardian.config.models import GuardianAppConfig
from docker_health_guardian.detection import HealthEvaluator
from docker_health_guardian.incidents import IncidentManager
from docker_health_guardian.models import (
    ContainerSnapshot,
    ContainerState,
    HealthStatus,
    NotificationOutcome,
    RemediationOutcome,
)
from docker_health_guardian.notifications import ConsoleChannel, NotificationManager
from docker_health_guardian.persistence import Database
from docker_health_guardian.remediation import RemediationEngine
from docker_health_guardian.scheduler import Scheduler


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


class FakeCollector:
    def __init__(self, snapshots=None, ping_result=True, list_error=None):
        self._snapshots = snapshots or []
        self._ping_result = ping_result
        self._list_error = list_error
        self.restart_calls = []

    def ping(self):
        return self._ping_result

    def list_container_snapshots(self):
        if self._list_error:
            raise self._list_error
        return self._snapshots

    def restart_container(self, name):
        self.restart_calls.append(name)

    def get_single_snapshot(self, name):
        for s in self._snapshots:
            if s.name == name:
                return s
        return None


def make_scheduler(db, collector, config=None):
    config = config or GuardianAppConfig()
    evaluator = HealthEvaluator(config.detection)
    incident_manager = IncidentManager(db)
    remediation_engine = RemediationEngine(db)
    notification_manager = NotificationManager(
        config.notifications, db, channels=[ConsoleChannel(config.notifications.console)]
    )
    return Scheduler(
        config, collector, evaluator, incident_manager, remediation_engine, notification_manager
    )


def unhealthy_snapshot():
    return ContainerSnapshot(
        container_id="id-app",
        name="app",
        image="demo",
        state=ContainerState.RUNNING,
        health_status=HealthStatus.UNHEALTHY,
    )


class TestRunOnceCreatesIncidents:
    def test_unhealthy_container_creates_incident_and_notifies(self, db):
        collector = FakeCollector(snapshots=[unhealthy_snapshot()])
        scheduler = make_scheduler(db, collector)
        scheduler.run_once()

        incidents = db.list_incidents()
        assert len(incidents) == 1
        assert incidents[0].container_name == "app"

        notifications = db.list_notification_records(incident_id=incidents[0].incident_id)
        assert len(notifications) == 1
        assert notifications[0].outcome == NotificationOutcome.SENT

    def test_denied_remediation_attempt_is_recorded_by_default(self, db):
        collector = FakeCollector(snapshots=[unhealthy_snapshot()])
        scheduler = make_scheduler(db, collector)
        scheduler.run_once()

        incident = db.list_incidents()[0]
        attempts = db.list_remediation_attempts(incident_id=incident.incident_id)
        assert len(attempts) == 1
        assert attempts[0].outcome == RemediationOutcome.DENIED
        assert collector.restart_calls == []

    def test_healthy_container_creates_no_incident(self, db):
        healthy = ContainerSnapshot(
            container_id="id-app",
            name="app",
            image="demo",
            state=ContainerState.RUNNING,
            health_status=HealthStatus.HEALTHY,
        )
        collector = FakeCollector(snapshots=[healthy])
        scheduler = make_scheduler(db, collector)
        scheduler.run_once()
        assert db.list_incidents() == []


class TestRecoveryClearsIncident:
    def test_incident_resolves_once_condition_clears(self, db):
        collector = FakeCollector(snapshots=[unhealthy_snapshot()])
        scheduler = make_scheduler(db, collector)
        scheduler.run_once()

        from docker_health_guardian.models import IncidentState

        healthy = ContainerSnapshot(
            container_id="id-app",
            name="app",
            image="demo",
            state=ContainerState.RUNNING,
            health_status=HealthStatus.HEALTHY,
        )
        collector._snapshots = [healthy]
        scheduler.run_once()

        incident = db.list_incidents()[0]
        assert incident.state == IncidentState.RESOLVED


class TestDaemonUnreachable:
    def test_unreachable_daemon_skips_container_collection(self, db):
        collector = FakeCollector(ping_result=False)
        scheduler = make_scheduler(db, collector)
        scheduler.run_once()  # should not raise even though no containers were ever listed

    def test_repeated_unreachable_cycles_create_system_incident(self, db):
        from docker_health_guardian.config.models import DetectionConfig

        config = GuardianAppConfig(detection=DetectionConfig(collection_failure_threshold=2))
        collector = FakeCollector(ping_result=False)
        scheduler = make_scheduler(db, collector, config=config)
        scheduler.run_once()
        scheduler.run_once()
        incidents = db.list_incidents()
        assert len(incidents) == 1
        assert incidents[0].rule_id == "docker_daemon_unreachable"

    def test_list_snapshots_error_does_not_crash_cycle(self, db):
        collector = FakeCollector(ping_result=True, list_error=DockerUnavailableError("boom"))
        scheduler = make_scheduler(db, collector)
        scheduler.run_once()  # must not raise
