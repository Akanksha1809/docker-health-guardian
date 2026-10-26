import pytest

from docker_health_guardian.diagnosis import diagnose
from docker_health_guardian.models import Confidence, RuleMatch, Severity


def match(rule_id: str, **evidence) -> RuleMatch:
    return RuleMatch(
        rule_id=rule_id,
        container_name="app",
        container_id="id-app",
        incident_type=rule_id,
        severity=Severity.HIGH,
        evidence=evidence,
    )


class TestEachRuleHasADiagnosis:
    @pytest.mark.parametrize(
        "rule_id",
        [
            "health_unhealthy",
            "unexpected_stop",
            "stopped_beyond_grace",
            "restart_loop",
            "startup_health_timeout",
            "cpu_sustained_high",
            "memory_sustained_high",
            "memory_growth",
            "metrics_unreliable",
            "docker_daemon_unreachable",
        ],
    )
    def test_returns_a_populated_diagnosis(self, rule_id):
        diagnosis = diagnose(match(rule_id))
        assert diagnosis.likely_cause
        assert diagnosis.recommended_action
        assert isinstance(diagnosis.safe_to_automate, bool)
        assert diagnosis.confidence in (Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH)


class TestConfidenceCapping:
    def test_threshold_rules_never_reach_high_confidence(self):
        for rule_id in ("cpu_sustained_high", "memory_sustained_high", "memory_growth"):
            diagnosis = diagnose(match(rule_id))
            assert diagnosis.confidence != Confidence.HIGH

    def test_authoritative_docker_signals_reach_high_confidence(self):
        for rule_id in ("health_unhealthy", "unexpected_stop", "restart_loop"):
            diagnosis = diagnose(match(rule_id))
            assert diagnosis.confidence == Confidence.HIGH


class TestSafeToAutomateFlags:
    def test_restart_loop_is_never_safe_to_automate(self):
        assert diagnose(match("restart_loop")).safe_to_automate is False

    def test_resource_pressure_rules_are_not_safe_to_automate(self):
        for rule_id in ("cpu_sustained_high", "memory_sustained_high", "memory_growth"):
            assert diagnose(match(rule_id)).safe_to_automate is False

    def test_container_health_rules_are_safe_to_automate(self):
        for rule_id in ("health_unhealthy", "unexpected_stop", "stopped_beyond_grace"):
            assert diagnose(match(rule_id)).safe_to_automate is True


class TestUnknownRuleFallsBackGracefully:
    def test_unrecognized_rule_id_still_returns_a_diagnosis(self):
        diagnosis = diagnose(match("some_future_rule_not_yet_mapped"))
        assert diagnosis.confidence == Confidence.LOW
        assert diagnosis.safe_to_automate is False


class TestEvidenceIsReflectedInDiagnosis:
    def test_exit_code_appears_in_unexpected_stop_diagnosis(self):
        diagnosis = diagnose(match("unexpected_stop", exit_code=137))
        assert "137" in diagnosis.likely_cause

    def test_restart_counts_appear_in_restart_loop_evidence(self):
        diagnosis = diagnose(match("restart_loop", restarts_in_window=5, window_seconds=300))
        assert any("5" in e for e in diagnosis.evidence)
