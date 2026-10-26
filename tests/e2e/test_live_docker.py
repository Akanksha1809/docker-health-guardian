"""End-to-end tests against a real Docker daemon using disposable,
uniquely-named `guardian-e2e-*` containers.

Opt-in: these are skipped unless Docker is reachable AND the environment
variable GUARDIAN_RUN_E2E=1 is set, since they create and remove real
containers (never anything outside the `guardian-e2e-` prefix) and take
several real seconds each due to Docker start/restart/healthcheck timing.

    GUARDIAN_RUN_E2E=1 pytest tests/e2e -q
"""

from __future__ import annotations

import os
import time
import uuid

import docker
import pytest

from docker_health_guardian.collector import DockerCollector
from docker_health_guardian.config.models import DetectionConfig, DockerConfig, RemediationConfig
from docker_health_guardian.detection import HealthEvaluator
from docker_health_guardian.incidents import IncidentManager
from docker_health_guardian.models import IncidentState
from docker_health_guardian.persistence import Database
from docker_health_guardian.remediation import RemediationEngine

pytestmark = pytest.mark.skipif(
    os.environ.get("GUARDIAN_RUN_E2E") != "1",
    reason="set GUARDIAN_RUN_E2E=1 to run live-Docker e2e tests (creates real containers)",
)

NAME_PREFIX = "guardian-e2e-"


def _unique_name() -> str:
    return f"{NAME_PREFIX}{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def docker_client():
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as exc:
        pytest.skip(f"docker daemon not reachable: {exc}")
    yield client


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


@pytest.fixture
def cleanup_containers(docker_client):
    created: list[str] = []
    yield created
    for name in created:
        try:
            container = docker_client.containers.get(name)
            container.remove(force=True)
        except docker.errors.NotFound:
            pass


def _wait_until(predicate, timeout_seconds=30, interval=1.0):
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class TestHealthyContainerStaysHealthy:
    def test_no_incident_for_a_healthy_container(self, docker_client, cleanup_containers, db):
        name = _unique_name()
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'while true; do sleep 1; done'",
            healthcheck={"test": ["CMD", "true"], "interval": 2_000_000_000, "retries": 1},
            detach=True,
            labels={"guardian.e2e": "true"},
        )
        cleanup_containers.append(name)

        collector = DockerCollector(DockerConfig(monitored_containers=[name]))
        evaluator = HealthEvaluator(DetectionConfig())
        incident_manager = IncidentManager(db)

        _wait_until(lambda: collector.get_single_snapshot(name) is not None, timeout_seconds=10)
        for _ in range(3):
            snapshot = collector.get_single_snapshot(name)
            triggered, cleared = evaluator.evaluate(snapshot)
            for match in triggered:
                incident_manager.process_triggered(match)
            time.sleep(1)

        assert db.list_incidents(container_name=name) == []


class TestUnhealthyContainerCreatesIncident:
    def test_unhealthy_healthcheck_raises_incident(self, docker_client, cleanup_containers, db):
        name = _unique_name()
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'while true; do sleep 1; done'",
            healthcheck={
                "test": ["CMD", "sh", "-c", "exit 1"],
                "interval": 1_000_000_000,
                "retries": 1,
                "start_period": 1_000_000_000,
            },
            detach=True,
            labels={"guardian.e2e": "true"},
        )
        cleanup_containers.append(name)

        collector = DockerCollector(DockerConfig(monitored_containers=[name]))
        evaluator = HealthEvaluator(DetectionConfig())
        incident_manager = IncidentManager(db)

        def observe_once() -> bool:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return False
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident_manager.process_triggered(match)
            return len(db.list_incidents(container_name=name)) > 0

        assert _wait_until(observe_once, timeout_seconds=30, interval=2.0)
        incidents = db.list_incidents(container_name=name)
        assert incidents[0].rule_id == "health_unhealthy"
        assert incidents[0].diagnosis is not None


class TestUnexpectedlyStoppedContainerIsDetected:
    def test_container_that_exits_is_flagged(self, docker_client, cleanup_containers, db):
        name = _unique_name()
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'sleep 2; exit 1'",
            detach=True,
            labels={"guardian.e2e": "true"},
        )
        cleanup_containers.append(name)

        collector = DockerCollector(DockerConfig(monitored_containers=[name]))
        evaluator = HealthEvaluator(DetectionConfig())
        incident_manager = IncidentManager(db)

        def observe_once() -> bool:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return False
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident_manager.process_triggered(match)
            return any(
                i.rule_id in ("unexpected_stop", "stopped_beyond_grace")
                for i in db.list_incidents(container_name=name)
            )

        assert _wait_until(observe_once, timeout_seconds=30, interval=1.0)


class TestMonitoringOnlyModeNeverRestarts:
    def test_default_remediation_config_never_calls_restart(
        self, docker_client, cleanup_containers, db
    ):
        name = _unique_name()
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'exit 1'",
            restart_policy={"Name": "always"},
            detach=True,
            labels={"guardian.e2e": "true"},
        )
        cleanup_containers.append(name)

        collector = DockerCollector(DockerConfig(monitored_containers=[name]))
        evaluator = HealthEvaluator(DetectionConfig(restart_count_threshold=2))
        incident_manager = IncidentManager(db)
        remediation_engine = RemediationEngine(db)
        cfg = RemediationConfig()  # default: enabled=False, dry_run=True

        def tick() -> None:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident = incident_manager.process_triggered(match)
                remediation_engine.attempt_remediation(incident, cfg, collector, incident_manager)

        start_restart_count = docker_client.containers.get(name).attrs["RestartCount"]
        for _ in range(8):
            tick()
            time.sleep(1.5)

        # Guardian never issued its own restart; only Docker's own restart
        # policy (which Guardian does not control) may have run.
        attempts = db.list_remediation_attempts(container_name=name)
        for attempt in attempts:
            assert attempt.outcome.value == "denied"
        assert start_restart_count >= 0  # sanity: attribute exists and is readable


class TestDryRunNeverMutatesLiveContainer:
    def test_dry_run_true_does_not_restart_even_when_allowlisted(
        self, docker_client, cleanup_containers, db
    ):
        name = _unique_name()
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'while true; do sleep 1; done'",
            healthcheck={
                "test": ["CMD", "sh", "-c", "exit 1"],
                "interval": 1_000_000_000,
                "retries": 1,
                "start_period": 1_000_000_000,
            },
            detach=True,
            labels={"guardian.e2e": "true"},
        )
        cleanup_containers.append(name)

        collector = DockerCollector(DockerConfig(monitored_containers=[name]))
        evaluator = HealthEvaluator(DetectionConfig())
        incident_manager = IncidentManager(db)
        remediation_engine = RemediationEngine(db)
        cfg = RemediationConfig(enabled=True, dry_run=True, restart_allowlist=[name])

        def tick() -> IncidentState | None:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return None
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident = incident_manager.process_triggered(match)
                remediation_engine.attempt_remediation(incident, cfg, collector, incident_manager)
            return None

        started_at_before = docker_client.containers.get(name).attrs["State"]["StartedAt"]
        for _ in range(10):
            tick()
            time.sleep(2)
        started_at_after = docker_client.containers.get(name).attrs["State"]["StartedAt"]

        assert started_at_before == started_at_after, "dry_run must never actually restart"
        attempts = db.list_remediation_attempts(container_name=name)
        assert any(a.outcome.value == "skipped" and a.dry_run for a in attempts)
