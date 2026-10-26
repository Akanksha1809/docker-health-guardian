"""Rule registry: identifier, severity, remediation permission, and the
re-raise cooldown for each detectable condition.

Each rule's actual detection/recovery logic lives in `evaluator.py`; this
module is the declarative, inspectable catalog (`guardian rules list`).
"""

from __future__ import annotations

from dataclasses import dataclass

from docker_health_guardian.models import Severity


@dataclass(frozen=True)
class RuleDefinition:
    rule_id: str
    description: str
    severity: Severity
    remediation_permitted: bool
    cooldown_seconds: float
    category: str


RULES: dict[str, RuleDefinition] = {
    "health_unhealthy": RuleDefinition(
        rule_id="health_unhealthy",
        description="Docker-reported health check status is 'unhealthy'",
        severity=Severity.HIGH,
        remediation_permitted=True,
        cooldown_seconds=180,
        category="container_health",
    ),
    "unexpected_stop": RuleDefinition(
        rule_id="unexpected_stop",
        description="A previously running container exited with a nonzero/unknown exit code",
        severity=Severity.HIGH,
        remediation_permitted=True,
        cooldown_seconds=180,
        category="container_health",
    ),
    "stopped_beyond_grace": RuleDefinition(
        rule_id="stopped_beyond_grace",
        description="Container has remained stopped beyond the configured grace period",
        severity=Severity.MEDIUM,
        remediation_permitted=True,
        cooldown_seconds=300,
        category="container_health",
    ),
    "restart_loop": RuleDefinition(
        rule_id="restart_loop",
        description="Container restarted repeatedly within the configured time window",
        severity=Severity.CRITICAL,
        remediation_permitted=False,
        cooldown_seconds=600,
        category="container_health",
    ),
    "startup_health_timeout": RuleDefinition(
        rule_id="startup_health_timeout",
        description="Container failed to become healthy within the startup grace period",
        severity=Severity.MEDIUM,
        remediation_permitted=True,
        cooldown_seconds=300,
        category="container_health",
    ),
    "cpu_sustained_high": RuleDefinition(
        rule_id="cpu_sustained_high",
        description="CPU usage exceeded threshold for the sustained duration",
        severity=Severity.MEDIUM,
        remediation_permitted=False,
        cooldown_seconds=300,
        category="resource_pressure",
    ),
    "memory_sustained_high": RuleDefinition(
        rule_id="memory_sustained_high",
        description="Memory utilization exceeded threshold for the sustained duration",
        severity=Severity.HIGH,
        remediation_permitted=False,
        cooldown_seconds=300,
        category="resource_pressure",
    ),
    "memory_growth": RuleDefinition(
        rule_id="memory_growth",
        description="Memory usage rose continuously across several collection intervals",
        severity=Severity.LOW,
        remediation_permitted=False,
        cooldown_seconds=600,
        category="resource_pressure",
    ),
    "metrics_unreliable": RuleDefinition(
        rule_id="metrics_unreliable",
        description="Resource metrics were unavailable or unreliable for several cycles",
        severity=Severity.LOW,
        remediation_permitted=False,
        cooldown_seconds=300,
        category="resource_pressure",
    ),
    "docker_daemon_unreachable": RuleDefinition(
        rule_id="docker_daemon_unreachable",
        description="The Docker daemon could not be reached for repeated collection cycles",
        severity=Severity.CRITICAL,
        remediation_permitted=False,
        cooldown_seconds=60,
        category="monitoring_reliability",
    ),
}


def get_rule(rule_id: str) -> RuleDefinition:
    try:
        return RULES[rule_id]
    except KeyError as exc:
        raise KeyError(f"unknown rule_id: {rule_id}") from exc
