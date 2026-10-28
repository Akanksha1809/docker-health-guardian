"""CLI tests via Typer's CliRunner -- exit codes and output for every command.

No real Docker daemon is used: DockerCollector is monkeypatched at its
defining module (docker_health_guardian.collector.DockerCollector) so the
commands' own local `from docker_health_guardian.collector import
DockerCollector` picks up the fake on each invocation.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from docker_health_guardian.cli.main import app
from docker_health_guardian.collector import DockerUnavailableError
from docker_health_guardian.models import (
    ContainerSnapshot,
    ContainerState,
    HealthStatus,
    Incident,
    RemediationAttempt,
    RemediationOutcome,
    ResourceStats,
    Severity,
)
from docker_health_guardian.persistence import Database

runner = CliRunner()


@pytest.fixture
def config_path(tmp_path):
    path = tmp_path / "guardian.yaml"
    db_path = tmp_path / "guardian.db"
    path.write_text(f"persistence:\n  db_path: {db_path}\n")
    return str(path)


@pytest.fixture
def db(config_path):
    import yaml

    with open(config_path) as f:
        raw = yaml.safe_load(f)
    database = Database(raw["persistence"]["db_path"])
    database.initialize()
    return database


def make_snapshot(name="app", state=ContainerState.RUNNING, health=HealthStatus.NONE, **stats):
    return ContainerSnapshot(
        container_id=f"id-{name}",
        name=name,
        image="demo:latest",
        state=state,
        health_status=health,
        stats=ResourceStats(**stats),
    )


class FakeCollector:
    def __init__(
        self, config=None, snapshots=None, ping_result=True, list_error=None, single=None
    ):
        self._snapshots = snapshots or []
        self._ping_result = ping_result
        self._list_error = list_error
        self._single = single

    def ping(self):
        return self._ping_result

    def list_container_snapshots(self):
        if self._list_error:
            raise self._list_error
        return self._snapshots

    def get_single_snapshot(self, name):
        return self._single


def patch_collector(monkeypatch, **kwargs):
    fake_factory = lambda config: FakeCollector(config, **kwargs)  # noqa: E731
    # `collector/__init__.py` is what the CLI's own lazy `from
    # docker_health_guardian.collector import DockerCollector` resolves...
    monkeypatch.setattr("docker_health_guardian.collector.DockerCollector", fake_factory)
    # ...but scheduler.py did `from ... import DockerCollector` at its own
    # top level, which bound its OWN name in scheduler's namespace at
    # import time -- patching the source module's attribute doesn't reach
    # an already-bound reference in another module, so `start`'s tests
    # (which go through build_scheduler) need this patched too, or they
    # silently fall through to a real Docker connection.
    monkeypatch.setattr("docker_health_guardian.scheduler.DockerCollector", fake_factory)


def make_incident(**overrides) -> Incident:
    defaults = dict(
        container_name="app",
        container_id="id-app",
        rule_id="health_unhealthy",
        incident_type="health_unhealthy",
        severity=Severity.HIGH,
    )
    defaults.update(overrides)
    return Incident(**defaults)


class TestContainers:
    def test_lists_snapshots_as_table(self, config_path, monkeypatch):
        patch_collector(monkeypatch, snapshots=[make_snapshot()])
        result = runner.invoke(app, ["containers", "--config", config_path])
        assert result.exit_code == 0
        assert "app" in result.output

    def test_json_output_is_valid_json(self, config_path, monkeypatch):
        patch_collector(monkeypatch, snapshots=[make_snapshot()])
        result = runner.invoke(app, ["containers", "--config", config_path, "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["name"] == "app"

    def test_invalid_config_exits_2(self, tmp_path, monkeypatch):
        bad = tmp_path / "bad.yaml"
        bad.write_text("detection:\n  cpu_threshold_percent: 999\n")
        result = runner.invoke(app, ["containers", "--config", str(bad)])
        assert result.exit_code == 2

    def test_docker_unreachable_exits_3(self, config_path, monkeypatch):
        patch_collector(monkeypatch, list_error=DockerUnavailableError("boom"))
        result = runner.invoke(app, ["containers", "--config", config_path])
        assert result.exit_code == 3


class TestConfigValidate:
    def test_valid_config_exits_0(self, config_path):
        result = runner.invoke(app, ["config-validate", "--config", config_path])
        assert result.exit_code == 0
        assert "is valid" in result.output

    def test_invalid_config_exits_2_with_message(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("guardian:\n  polling_interval_seconds: -1\n")
        result = runner.invoke(app, ["config-validate", "--config", str(bad)])
        assert result.exit_code == 2
        assert "Invalid configuration" in result.output


class TestStatus:
    def test_reports_mode_and_open_incidents(self, config_path, monkeypatch):
        patch_collector(monkeypatch, snapshots=[make_snapshot()])
        result = runner.invoke(app, ["status", "--config", config_path])
        assert result.exit_code == 0
        assert "open incidents: 0" in result.output
        assert "containers observed: 1" in result.output

    def test_docker_unreachable_does_not_crash(self, config_path, monkeypatch):
        patch_collector(monkeypatch, list_error=DockerUnavailableError("boom"))
        result = runner.invoke(app, ["status", "--config", config_path])
        assert result.exit_code == 0
        assert "unreachable" in result.output


class TestSelfCheck:
    def test_all_checks_pass_exits_0(self, config_path, monkeypatch):
        patch_collector(monkeypatch, ping_result=True)
        result = runner.invoke(app, ["self-check", "--config", config_path])
        assert result.exit_code == 0
        assert "All checks passed" in result.output

    def test_docker_unreachable_exits_1(self, config_path, monkeypatch):
        patch_collector(monkeypatch, ping_result=False)
        result = runner.invoke(app, ["self-check", "--config", config_path])
        assert result.exit_code == 1

    def test_invalid_config_exits_2(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("guardian:\n  log_level: NOT_A_LEVEL\n")
        result = runner.invoke(app, ["self-check", "--config", str(bad)])
        assert result.exit_code == 2

    def test_docker_collector_raising_is_reported_not_crashed(self, config_path, monkeypatch):
        def raising_collector(config):
            raise RuntimeError("socket permission denied")

        monkeypatch.setattr("docker_health_guardian.collector.DockerCollector", raising_collector)
        result = runner.invoke(app, ["self-check", "--config", config_path])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "socket permission denied" in result.output

    def test_persistence_failure_is_reported_not_crashed(self, config_path, monkeypatch):
        patch_collector(monkeypatch, ping_result=True)

        def raising_initialize(self):
            raise RuntimeError("disk full")

        monkeypatch.setattr(Database, "initialize", raising_initialize)
        result = runner.invoke(app, ["self-check", "--config", config_path])
        assert result.exit_code == 1
        assert "FAIL" in result.output
        assert "disk full" in result.output


class TestRules:
    def test_list_as_table(self):
        result = runner.invoke(app, ["rules", "list"])
        assert result.exit_code == 0
        assert "restart_loop" in result.output

    def test_list_json_contains_all_rules(self):
        result = runner.invoke(app, ["rules", "list", "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert len(parsed) == 10
        assert {r["rule_id"] for r in parsed} >= {"health_unhealthy", "restart_loop"}

    def test_validate_valid_config(self, config_path):
        result = runner.invoke(app, ["rules", "validate", "--config", config_path])
        assert result.exit_code == 0

    def test_validate_invalid_config_exits_2(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("detection:\n  restart_count_threshold: 0\n")
        result = runner.invoke(app, ["rules", "validate", "--config", str(bad)])
        assert result.exit_code == 2


class TestIncidentsList:
    def test_empty_list(self, config_path):
        result = runner.invoke(app, ["incidents", "list", "--config", config_path])
        assert result.exit_code == 0

    def test_lists_a_created_incident(self, config_path, db):
        db.upsert_incident(make_incident())
        result = runner.invoke(app, ["incidents", "list", "--config", config_path])
        assert result.exit_code == 0
        assert "app" in result.output

    def test_filters_by_severity(self, config_path, db):
        db.upsert_incident(make_incident(severity=Severity.LOW))
        db.upsert_incident(make_incident(container_name="app2", severity=Severity.CRITICAL))
        result = runner.invoke(
            app, ["incidents", "list", "--config", config_path, "--severity", "critical"]
        )
        assert result.exit_code == 0
        assert "app2" in result.output
        assert "app" not in result.output.replace("app2", "")

    def test_json_output(self, config_path, db):
        db.upsert_incident(make_incident())
        result = runner.invoke(app, ["incidents", "list", "--config", config_path, "--json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["container_name"] == "app"


class TestIncidentShow:
    def test_shows_existing_incident(self, config_path, db):
        incident = make_incident()
        db.upsert_incident(incident)
        result = runner.invoke(app, ["incident", incident.incident_id, "--config", config_path])
        assert result.exit_code == 0
        assert incident.incident_id in result.output

    def test_missing_incident_exits_1(self, config_path, db):
        result = runner.invoke(app, ["incident", "does-not-exist", "--config", config_path])
        assert result.exit_code == 1
        assert "No incident found" in result.output

    def test_json_includes_events_and_attempts(self, config_path, db):
        incident = make_incident()
        db.upsert_incident(incident)
        result = runner.invoke(
            app, ["incident", incident.incident_id, "--config", config_path, "--json"]
        )
        parsed = json.loads(result.output)
        assert "events" in parsed
        assert "remediation_attempts" in parsed

    def test_displays_diagnosis_when_present(self, config_path, db):
        from docker_health_guardian.models import Confidence, Diagnosis

        incident = make_incident()
        incident.diagnosis = Diagnosis(
            likely_cause="test likely cause",
            confidence=Confidence.HIGH,
            recommended_action="do the thing",
            safe_to_automate=True,
        )
        db.upsert_incident(incident)
        result = runner.invoke(app, ["incident", incident.incident_id, "--config", config_path])
        assert result.exit_code == 0
        assert "test likely cause" in result.output
        assert "do the thing" in result.output

    def test_displays_remediation_attempts_table_when_present(self, config_path, db):
        incident = make_incident()
        db.upsert_incident(incident)
        db.upsert_remediation_attempt(
            RemediationAttempt(
                incident_id=incident.incident_id,
                container_name="app",
                action="restart",
                dry_run=True,
                reason="test",
                outcome=RemediationOutcome.SKIPPED,
            )
        )
        result = runner.invoke(app, ["incident", incident.incident_id, "--config", config_path])
        assert result.exit_code == 0
        assert "Remediation Attempts" in result.output


class TestRemediationHistory:
    def test_empty_history(self, config_path):
        result = runner.invoke(app, ["remediation", "history", "--config", config_path])
        assert result.exit_code == 0

    def test_shows_attempt(self, config_path, db):
        incident = make_incident()
        db.upsert_incident(incident)
        db.upsert_remediation_attempt(
            RemediationAttempt(
                incident_id=incident.incident_id,
                container_name="app",
                action="restart",
                dry_run=True,
                reason="test",
                outcome=RemediationOutcome.SKIPPED,
            )
        )
        result = runner.invoke(app, ["remediation", "history", "--config", config_path])
        assert result.exit_code == 0
        assert "app" in result.output

    def test_json_output(self, config_path, db):
        incident = make_incident()
        db.upsert_incident(incident)
        db.upsert_remediation_attempt(
            RemediationAttempt(
                incident_id=incident.incident_id,
                container_name="app",
                action="restart",
                dry_run=True,
                reason="test",
                outcome=RemediationOutcome.SKIPPED,
            )
        )
        result = runner.invoke(
            app, ["remediation", "history", "--config", config_path, "--json"]
        )
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["container_name"] == "app"


class TestRemediationDryRun:
    def test_not_eligible_by_default(self, config_path):
        result = runner.invoke(app, ["remediation", "dry-run", "app", "--config", config_path])
        assert result.exit_code == 0
        assert "NOT ELIGIBLE" in result.output
        assert "monitoring-only" in result.output


class TestRemediationEnableDisable:
    def test_enable_persists_override(self, config_path, db):
        result = runner.invoke(app, ["remediation", "enable", "app", "--config", config_path])
        assert result.exit_code == 0
        assert "enabled" in result.output.lower()
        assert db.get_container_override("app") is True

    def test_disable_persists_override(self, config_path, db):
        result = runner.invoke(app, ["remediation", "disable", "app", "--config", config_path])
        assert result.exit_code == 0
        assert db.get_container_override("app") is False


class TestReport:
    def test_json_report_to_stdout(self, config_path, db):
        db.upsert_incident(make_incident())
        result = runner.invoke(app, ["report", "--config", config_path, "--format", "json"])
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed[0]["container_name"] == "app"

    def test_writes_to_output_file(self, config_path, db, tmp_path):
        db.upsert_incident(make_incident())
        out_file = tmp_path / "report.csv"
        result = runner.invoke(
            app,
            [
                "report",
                "--config",
                config_path,
                "--format",
                "csv",
                "--output",
                str(out_file),
            ],
        )
        assert result.exit_code == 0
        assert out_file.exists()
        assert "app" in out_file.read_text()


class TestCleanup:
    def test_reports_zero_when_nothing_to_remove(self, config_path):
        result = runner.invoke(app, ["cleanup", "--config", config_path])
        assert result.exit_code == 0
        assert "Removed 0 incident" in result.output

    def test_removes_old_resolved_incidents(self, config_path, db):
        from datetime import timedelta

        from docker_health_guardian.models import IncidentState, utcnow

        old = make_incident()
        old.state = IncidentState.RESOLVED
        old.resolved_at = utcnow() - timedelta(days=100)
        db.upsert_incident(old)

        result = runner.invoke(app, ["cleanup", "--config", config_path])
        assert result.exit_code == 0
        assert "Removed 1 incident" in result.output


class TestStart:
    def test_invalid_config_exits_2(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("remediation:\n  max_actions_per_container: -1\n")
        result = runner.invoke(app, ["start", "--config", str(bad)])
        assert result.exit_code == 2

    def test_wires_up_and_runs_one_cycle_then_returns(self, config_path, monkeypatch):
        """Full integration of the `start` command's wiring, without an infinite
        loop or a real API server: build_scheduler is real, only run_forever
        is replaced with a single run_once() call."""
        patch_collector(monkeypatch, snapshots=[])

        import yaml

        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}
        raw.setdefault("api", {})["enabled"] = False
        with open(config_path, "w") as f:
            yaml.safe_dump(raw, f)

        from docker_health_guardian.scheduler import Scheduler

        original_run_forever = Scheduler.run_forever

        def run_once_and_return(self):
            self.run_once()

        monkeypatch.setattr(Scheduler, "run_forever", run_once_and_return)
        result = runner.invoke(app, ["start", "--config", config_path])
        assert result.exit_code == 0
        monkeypatch.setattr(Scheduler, "run_forever", original_run_forever)

    def test_wires_up_api_server_when_enabled(self, config_path, monkeypatch):
        """Covers the api.enabled branch: a fake uvicorn.Server stands in for
        the real one so no port is actually bound."""
        patch_collector(monkeypatch, snapshots=[])

        from docker_health_guardian.scheduler import Scheduler

        monkeypatch.setattr(Scheduler, "run_forever", lambda self: self.run_once())

        class FakeUvicornServer:
            def __init__(self, config):
                self.should_exit = False

            def run(self):
                pass

        import uvicorn

        monkeypatch.setattr(uvicorn, "Server", FakeUvicornServer)

        result = runner.invoke(app, ["start", "--config", config_path])
        assert result.exit_code == 0
        assert "API listening" in result.output
