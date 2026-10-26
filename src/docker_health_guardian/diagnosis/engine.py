"""Deterministic, explainable diagnosis from a RuleMatch's own evidence.

No ML/heuristic guessing: every diagnosis is a direct, documented mapping
from a detection rule (plus its evidence dict) to a likely cause, confidence,
and recommendation. "High" confidence is reserved for rules backed by
Docker's own authoritative signals (health status, exit code, daemon
reachability); threshold-based resource rules are capped at "medium" because
a sustained breach is evidence of pressure, not proof of a specific cause.
"""

from __future__ import annotations

from docker_health_guardian.models import Confidence, Diagnosis, RuleMatch


def diagnose(match: RuleMatch) -> Diagnosis:
    builder = _DIAGNOSES.get(match.rule_id, _unknown_rule)
    return builder(match)


def _health_unhealthy(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Application-level health check is failing inside the container",
        confidence=Confidence.HIGH,
        evidence=[f"Docker health status reported as '{match.evidence.get('health_status')}'"],
        alternative_explanations=[
            "Health check command itself is misconfigured or has a too-short timeout",
            "A dependency the health check calls (DB, cache, upstream API) is down",
        ],
        recommended_action="Inspect recent container logs; restart if the app is known to "
        "recover cleanly, otherwise escalate",
        safe_to_automate=True,
    )


def _unexpected_stop(match: RuleMatch) -> Diagnosis:
    exit_code = match.evidence.get("exit_code")
    return Diagnosis(
        likely_cause=f"Process inside the container exited unexpectedly (exit code {exit_code})",
        confidence=Confidence.HIGH,
        evidence=[f"State transitioned from running to stopped with exit_code={exit_code}"],
        alternative_explanations=[
            "Out-of-memory kill by the kernel (check exit code 137 / OOM in logs)",
            "Uncaught exception or panic in the application",
            "Deliberate stop/shutdown issued outside Guardian",
        ],
        recommended_action="Capture a log excerpt before any restart to preserve failure evidence",
        safe_to_automate=True,
    )


def _stopped_beyond_grace(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Container did not restart or was not restarted after stopping",
        confidence=Confidence.MEDIUM,
        evidence=[f"Stopped for {match.evidence.get('stopped_for_seconds')}s"],
        alternative_explanations=[
            "Intentionally stopped by an operator and simply not yet excluded from monitoring",
            "Restart policy is 'no' or 'on-failure' and the exit was clean",
        ],
        recommended_action="Confirm whether this container is expected to be running; restart "
        "if eligible, otherwise add it to excluded_containers",
        safe_to_automate=True,
    )


def _restart_loop(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Container is crash-looping -- it starts, fails, and restarts repeatedly",
        confidence=Confidence.HIGH,
        evidence=[
            f"{match.evidence.get('restarts_in_window')} restarts within "
            f"{match.evidence.get('window_seconds')}s"
        ],
        alternative_explanations=[
            "Misconfiguration causing immediate startup failure (bad env var, missing secret)",
            "A dependency the app requires at startup is unavailable",
        ],
        recommended_action="Do not restart again automatically -- capture logs and escalate to "
        "a human; repeated restarts will not fix a startup-time failure",
        safe_to_automate=False,
    )


def _startup_health_timeout(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Container is running but never reached a healthy state after starting",
        confidence=Confidence.MEDIUM,
        evidence=[f"{match.evidence.get('seconds_since_start')}s since start, still 'starting'"],
        alternative_explanations=[
            "Slow application warm-up exceeding the configured health-check start period",
            "A required downstream dependency is unreachable from within the container",
        ],
        recommended_action="Check whether the health check's start_period is too short for this "
        "workload before restarting",
        safe_to_automate=True,
    )


def _cpu_sustained_high(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Sustained CPU saturation",
        confidence=Confidence.MEDIUM,
        evidence=[
            f"CPU at {match.evidence.get('cpu_percent')}% for "
            f"{match.evidence.get('exceeded_for_seconds')}s"
        ],
        alternative_explanations=[
            "Legitimate load increase (traffic spike) rather than a defect",
            "A runaway loop or inefficient query introduced by a recent change",
            "Missing CPU limit allowing one container to starve others",
        ],
        recommended_action="Notify operator; a restart does not fix sustained load and is not "
        "attempted automatically for this rule",
        safe_to_automate=False,
    )


def _memory_sustained_high(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Memory utilization sustained near the configured limit",
        confidence=Confidence.MEDIUM,
        evidence=[
            f"Memory at {match.evidence.get('memory_percent')}% for "
            f"{match.evidence.get('exceeded_for_seconds')}s"
        ],
        alternative_explanations=[
            "Legitimately memory-intensive workload with a limit set too low",
            "A slow memory leak (check the memory_growth rule for corroborating evidence)",
        ],
        recommended_action="Notify operator; review memory_limit configuration or investigate "
        "for a leak before any restart",
        safe_to_automate=False,
    )


def _memory_growth(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Possible memory leak -- usage has risen continuously across samples",
        confidence=Confidence.LOW,
        evidence=[f"Rising samples (bytes): {match.evidence.get('samples_bytes')}"],
        alternative_explanations=[
            "Expected cache warm-up that will plateau",
            "Workload genuinely holding more data in memory over time",
        ],
        recommended_action="Keep observing; do not restart on this signal alone -- escalate if "
        "it continues toward the memory limit",
        safe_to_automate=False,
    )


def _metrics_unreliable(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="Resource metrics collection is failing for this container",
        confidence=Confidence.MEDIUM,
        evidence=[
            f"{match.evidence.get('consecutive_failures')} consecutive unreliable samples "
            f"({match.evidence.get('reason')})"
        ],
        alternative_explanations=[
            "Container just started and has not produced a second CPU sample yet",
            "Docker API is under load or the container's cgroup is in a transitional state",
        ],
        recommended_action="Treat resource-based rules for this container as untrustworthy "
        "until metrics stabilize; do not infer resource pressure from absence of data",
        safe_to_automate=False,
    )


def _docker_daemon_unreachable(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause="The monitoring infrastructure itself cannot reach the Docker daemon",
        confidence=Confidence.HIGH,
        evidence=[f"{match.evidence.get('consecutive_failures')} consecutive failed pings"],
        alternative_explanations=[
            "Docker daemon is restarting or under heavy load",
            "Socket permissions changed or the socket path is no longer valid",
        ],
        recommended_action="This is a monitoring-infrastructure failure, not a container "
        "failure -- no container remediation applies; escalate immediately",
        safe_to_automate=False,
    )


def _unknown_rule(match: RuleMatch) -> Diagnosis:
    return Diagnosis(
        likely_cause=f"Unrecognized rule '{match.rule_id}' fired",
        confidence=Confidence.LOW,
        evidence=[str(match.evidence)],
        alternative_explanations=[],
        recommended_action="Escalate to an operator for manual investigation",
        safe_to_automate=False,
    )


_DIAGNOSES = {
    "health_unhealthy": _health_unhealthy,
    "unexpected_stop": _unexpected_stop,
    "stopped_beyond_grace": _stopped_beyond_grace,
    "restart_loop": _restart_loop,
    "startup_health_timeout": _startup_health_timeout,
    "cpu_sustained_high": _cpu_sustained_high,
    "memory_sustained_high": _memory_sustained_high,
    "memory_growth": _memory_growth,
    "metrics_unreliable": _metrics_unreliable,
    "docker_daemon_unreachable": _docker_daemon_unreachable,
}
