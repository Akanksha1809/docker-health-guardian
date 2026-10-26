
import pytest

from docker_health_guardian.config.models import RemediationConfig
from docker_health_guardian.incidents import IncidentManager
from docker_health_guardian.models import (
    IncidentState,
    RemediationOutcome,
    RuleMatch,
    Severity,
)
from docker_health_guardian.persistence import Database
from docker_health_guardian.remediation import RemediationEngine


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


@pytest.fixture
def incident_manager(db):
    return IncidentManager(db)


def make_incident(incident_manager, rule_id="health_unhealthy", container="app"):
    match = RuleMatch(
        rule_id=rule_id,
        container_name=container,
        container_id=f"id-{container}",
        incident_type=rule_id,
        severity=Severity.HIGH,
        evidence={},
    )
    return incident_manager.process_triggered(match)


class FakeCollector:
    def __init__(self, restart_error: Exception | None = None, post_restart_state="RUNNING"):
        self.restart_calls: list[str] = []
        self._restart_error = restart_error
        self._post_restart_state = post_restart_state

    def restart_container(self, name: str) -> None:
        self.restart_calls.append(name)
        if self._restart_error:
            raise self._restart_error

    def get_single_snapshot(self, name: str):
        from docker_health_guardian.models import ContainerSnapshot, ContainerState, HealthStatus

        state = (
            ContainerState.RUNNING
            if self._post_restart_state == "RUNNING"
            else ContainerState.RESTARTING
        )
        return ContainerSnapshot(
            container_id="id",
            name=name,
            image="demo",
            state=state,
            health_status=(
                HealthStatus.HEALTHY if state == ContainerState.RUNNING else HealthStatus.NONE
            ),
        )


def permissive_config(**overrides) -> RemediationConfig:
    defaults = dict(
        enabled=True,
        dry_run=False,
        cooldown_seconds=0,
        max_actions_per_container=5,
        allowed_actions=["capture_logs", "restart"],
        restart_allowlist=["app"],
        protected_containers=[],
        recovery_grace_period_seconds=1,
    )
    defaults.update(overrides)
    return RemediationConfig(**defaults)


class TestMonitoringOnlyIsSafeByDefault:
    def test_denied_when_remediation_disabled(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        cfg = RemediationConfig()  # default: enabled=False, dry_run=True
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "monitoring-only" in reason

    def test_default_config_never_calls_docker_restart(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector()
        attempt = engine.attempt_remediation(
            incident, RemediationConfig(), collector, incident_manager
        )
        assert attempt.outcome == RemediationOutcome.DENIED
        assert collector.restart_calls == []


class TestDryRunNeverMutates:
    def test_dry_run_true_records_skip_and_does_not_restart(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector()
        cfg = permissive_config(dry_run=True)
        attempt = engine.attempt_remediation(incident, cfg, collector, incident_manager)
        assert attempt.outcome == RemediationOutcome.SKIPPED
        assert attempt.dry_run is True
        assert collector.restart_calls == []


class TestAllowlistEnforcement:
    def test_denied_when_not_in_allowlist(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager, container="other-app")
        cfg = permissive_config(restart_allowlist=["app"])
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "allowlist" in reason

    def test_denied_when_allowlist_empty(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        cfg = permissive_config(restart_allowlist=[])
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "empty" in reason

    def test_denied_when_protected(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        # protected_containers always wins; it is checked before the allowlist,
        # and the config model itself forbids a container being in both lists.
        cfg = permissive_config(restart_allowlist=[], protected_containers=["app"])
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "protected" in reason


class TestRulePermission:
    def test_denied_when_rule_does_not_permit_remediation(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager, rule_id="restart_loop")
        cfg = permissive_config()
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "does not permit" in reason


class TestSuccessfulRestart:
    def test_executes_restart_and_verifies_recovery(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector(post_restart_state="RUNNING")
        cfg = permissive_config()
        attempt = engine.attempt_remediation(incident, cfg, collector, incident_manager)
        assert attempt.outcome == RemediationOutcome.SUCCESS
        assert attempt.recovery_verified is True
        assert collector.restart_calls == ["app"]
        fetched = db.get_incident(incident.incident_id)
        assert fetched.state == IncidentState.VERIFYING

    def test_restart_command_failure_is_recorded(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector(restart_error=RuntimeError("daemon unreachable"))
        cfg = permissive_config()
        attempt = engine.attempt_remediation(incident, cfg, collector, incident_manager)
        assert attempt.outcome == RemediationOutcome.FAILED
        assert "daemon unreachable" in attempt.detail
        fetched = db.get_incident(incident.incident_id)
        assert fetched.state == IncidentState.REMEDIATION_FAILED

    def test_recovery_not_verified_marks_remediation_failed(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector(post_restart_state="RESTARTING")
        cfg = permissive_config(recovery_grace_period_seconds=0.01)
        attempt = engine.attempt_remediation(incident, cfg, collector, incident_manager)
        assert attempt.outcome == RemediationOutcome.FAILED
        assert attempt.recovery_verified is False
        fetched = db.get_incident(incident.incident_id)
        assert fetched.state == IncidentState.REMEDIATION_FAILED


class TestCooldownAndMaxActions:
    def test_cooldown_blocks_immediate_second_attempt(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector()
        cfg = permissive_config(cooldown_seconds=300)
        engine.attempt_remediation(incident, cfg, collector, incident_manager)
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "cooldown" in reason

    def test_max_actions_per_container_blocks_further_attempts(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector()
        cfg = permissive_config(cooldown_seconds=0, max_actions_per_container=1)
        engine.attempt_remediation(incident, cfg, collector, incident_manager)
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "max_actions_per_container" in reason

    def test_dry_run_attempts_do_not_count_toward_max_actions(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector()
        dry_cfg = permissive_config(dry_run=True, max_actions_per_container=1)
        engine.attempt_remediation(incident, dry_cfg, collector, incident_manager)
        real_cfg = permissive_config(dry_run=False, max_actions_per_container=1, cooldown_seconds=0)
        eligible, _ = engine.check_eligibility(incident, real_cfg)
        assert eligible is True


class TestContainerOverrides:
    def test_disable_override_denies_even_if_allowlisted(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        db.set_container_override("app", enabled=False)
        cfg = permissive_config()
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "disabled" in reason

    def test_enable_override_allows_even_without_static_allowlist(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        db.set_container_override("app", enabled=True)
        cfg = permissive_config(restart_allowlist=[])
        eligible, _ = engine.check_eligibility(incident, cfg)
        assert eligible is True

    def test_enable_override_does_not_bypass_protected_containers(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        db.set_container_override("app", enabled=True)
        cfg = permissive_config(restart_allowlist=[], protected_containers=["app"])
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "protected" in reason

    def test_clear_override_reverts_to_static_config(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        db.set_container_override("app", enabled=False)
        db.clear_container_override("app")
        cfg = permissive_config()
        eligible, _ = engine.check_eligibility(incident, cfg)
        assert eligible is True


class TestConcurrencyLock:
    def test_in_progress_container_is_not_double_remediated(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        engine._in_progress.add("app")
        cfg = permissive_config()
        eligible, reason = engine.check_eligibility(incident, cfg)
        assert eligible is False
        assert "already in progress" in reason


class TestEveryAttemptIsAudited:
    def test_denied_attempts_are_persisted(self, db, incident_manager):
        engine = RemediationEngine(db)
        incident = make_incident(incident_manager)
        collector = FakeCollector()
        engine.attempt_remediation(incident, RemediationConfig(), collector, incident_manager)
        attempts = db.list_remediation_attempts(incident_id=incident.incident_id)
        assert len(attempts) == 1
        assert attempts[0].outcome == RemediationOutcome.DENIED
