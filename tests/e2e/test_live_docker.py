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


class TestLiveRestartIsActuallyIssuedAndRecoveryVerified:
    """The remediation path with real Docker, real dry_run=false: a restart
    actually happens, and recovery is verified against the container's real
    state -- not assumed from the restart command's own success."""

    def test_allowlisted_container_is_restarted_and_recovery_is_verified(
        self, docker_client, cleanup_containers, db
    ):
        name = _unique_name()
        # No healthcheck: once restarted, RUNNING alone counts as recovered.
        # Exits on its own after 4s so the container is genuinely stopped
        # (triggering a real unexpected_stop incident) before any
        # remediation is attempted -- not a container we spin up already
        # healthy and restart for no organic reason.
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'sleep 4; exit 1'",
            detach=True,
            labels={"guardian.e2e": "true"},
        )
        cleanup_containers.append(name)

        collector = DockerCollector(DockerConfig(monitored_containers=[name]))
        evaluator = HealthEvaluator(DetectionConfig())
        incident_manager = IncidentManager(db)
        remediation_engine = RemediationEngine(db)
        cfg = RemediationConfig(
            enabled=True,
            dry_run=False,
            restart_allowlist=[name],
            cooldown_seconds=0,
            max_actions_per_container=5,
            recovery_grace_period_seconds=15,
        )

        incident_holder: dict[str, object] = {}

        def wait_for_unexpected_stop_incident() -> bool:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return False
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident = incident_manager.process_triggered(match)
                if match.rule_id == "unexpected_stop":
                    incident_holder["incident"] = incident
            return "incident" in incident_holder

        assert _wait_until(wait_for_unexpected_stop_incident, timeout_seconds=30, interval=1.0)
        incident = incident_holder["incident"]
        started_at_before = docker_client.containers.get(name).attrs["State"]["StartedAt"]

        attempt = remediation_engine.attempt_remediation(
            incident, cfg, collector, incident_manager
        )

        assert attempt.outcome.value == "success"
        assert attempt.recovery_verified is True
        started_at_after = docker_client.containers.get(name).attrs["State"]["StartedAt"]
        assert started_at_after != started_at_before, "Guardian must have actually restarted it"

        fetched = db.get_incident(incident.incident_id)
        assert fetched.state == IncidentState.VERIFYING, (
            "the engine itself must never mark RESOLVED -- only a later "
            "evaluator observation does"
        )

        # Closing the loop: the next real observation while the container is
        # genuinely running again resolves the incident through the normal
        # evaluator path, exactly as in production.
        def observe_resolution() -> bool:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None or snapshot.state.value != "running":
                return False
            _, cleared = evaluator.evaluate(snapshot)
            for rule_id in cleared:
                incident_manager.process_cleared(name, rule_id)
            resolved = db.get_incident(incident.incident_id)
            return resolved.state == IncidentState.RESOLVED

        assert _wait_until(observe_resolution, timeout_seconds=10, interval=0.5)


class TestLiveRemediationFailureWhenHealthNeverRecovers:
    """A restart that succeeds at the Docker-API level but whose container
    never reports healthy must be recorded as failed, never as resolved."""

    def test_restart_without_recovery_marks_remediation_failed(
        self, docker_client, cleanup_containers, db
    ):
        name = _unique_name()
        docker_client.containers.run(
            "busybox:1.36",
            name=name,
            command="sh -c 'while true; do sleep 1; done'",
            healthcheck={
                "test": ["CMD", "sh", "-c", "exit 1"],  # always fails, restart can't fix this
                "interval": 1_000_000_000,
                "timeout": 1_000_000_000,
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
        cfg = RemediationConfig(
            enabled=True,
            dry_run=False,
            restart_allowlist=[name],
            cooldown_seconds=0,
            max_actions_per_container=5,
            # Shorter than this healthcheck can possibly report "healthy" --
            # it never will, since the check itself always fails -- so this
            # deterministically proves the failure path, not a flaky race.
            recovery_grace_period_seconds=4,
        )

        incident_holder: dict[str, object] = {}

        def wait_for_unhealthy_incident() -> bool:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return False
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident = incident_manager.process_triggered(match)
                if match.rule_id == "health_unhealthy":
                    incident_holder["incident"] = incident
            return "incident" in incident_holder

        assert _wait_until(wait_for_unhealthy_incident, timeout_seconds=30, interval=1.0)
        incident = incident_holder["incident"]

        attempt = remediation_engine.attempt_remediation(
            incident, cfg, collector, incident_manager
        )

        assert attempt.outcome.value == "failed"
        assert attempt.recovery_verified is False
        fetched = db.get_incident(incident.incident_id)
        assert fetched.state == IncidentState.REMEDIATION_FAILED


class TestRestartLoopIsDeniedEvenWhenFullyArmed:
    """restart_loop must stay undeniable even with remediation fully enabled
    and the container explicitly allowlisted -- the rule itself, not just
    the global monitoring-only switch, is what blocks it."""

    def test_crash_looping_container_is_never_restarted_by_guardian(
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
        cfg = RemediationConfig(
            enabled=True,
            dry_run=False,
            restart_allowlist=[name],
            cooldown_seconds=0,
            max_actions_per_container=5,
        )

        def tick() -> None:
            snapshot = collector.get_single_snapshot(name)
            if snapshot is None:
                return
            triggered, _ = evaluator.evaluate(snapshot)
            for match in triggered:
                incident = incident_manager.process_triggered(match)
                remediation_engine.attempt_remediation(incident, cfg, collector, incident_manager)

        for _ in range(8):
            tick()
            time.sleep(1.5)

        attempts = db.list_remediation_attempts(container_name=name)
        restart_loop_incident_ids = {
            i.incident_id
            for i in db.list_incidents(container_name=name)
            if i.rule_id == "restart_loop"
        }
        assert restart_loop_incident_ids, "expected a restart_loop incident to have been raised"
        restart_loop_attempts = [a for a in attempts if a.incident_id in restart_loop_incident_ids]
        assert restart_loop_attempts, "expected remediation to have been attempted and denied"
        for attempt in restart_loop_attempts:
            assert attempt.outcome.value == "denied"
