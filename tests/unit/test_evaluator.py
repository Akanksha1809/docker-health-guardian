from datetime import UTC, datetime, timedelta

from docker_health_guardian.config.models import DetectionConfig
from docker_health_guardian.detection.evaluator import HealthEvaluator
from docker_health_guardian.models import (
    ContainerSnapshot,
    ContainerState,
    HealthStatus,
    ResourceStats,
)

BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def snap(
    name="app",
    state=ContainerState.RUNNING,
    health=HealthStatus.NONE,
    t=0,
    restart_count=0,
    exit_code=None,
    cpu=None,
    mem_percent=None,
    mem_bytes=None,
    reliable=True,
    started_at=None,
    excluded=False,
):
    return ContainerSnapshot(
        container_id=f"id-{name}",
        name=name,
        image="demo:latest",
        state=state,
        health_status=health,
        started_at=started_at,
        exit_code=exit_code,
        restart_count=restart_count,
        stats=ResourceStats(
            cpu_percent=cpu,
            memory_percent=mem_percent,
            memory_usage_bytes=mem_bytes,
            reliable=reliable,
        ),
        observed_at=BASE_TIME + timedelta(seconds=t),
        monitoring_excluded=excluded,
    )


def rule_ids(matches):
    return {m.rule_id for m in matches}


class TestHealthUnhealthy:
    def test_fires_immediately_on_unhealthy_status(self):
        ev = HealthEvaluator(DetectionConfig())
        triggered, _ = ev.evaluate(snap(health=HealthStatus.UNHEALTHY))
        assert "health_unhealthy" in rule_ids(triggered)

    def test_clears_when_healthy_again(self):
        ev = HealthEvaluator(DetectionConfig())
        ev.evaluate(snap(health=HealthStatus.UNHEALTHY))
        _, cleared = ev.evaluate(snap(health=HealthStatus.HEALTHY, t=15))
        assert "health_unhealthy" in cleared


class TestUnexpectedStop:
    def test_fires_on_running_to_exited_with_nonzero_code(self):
        ev = HealthEvaluator(DetectionConfig())
        ev.evaluate(snap(state=ContainerState.RUNNING, t=0))
        triggered, _ = ev.evaluate(snap(state=ContainerState.EXITED, exit_code=137, t=5))
        assert "unexpected_stop" in rule_ids(triggered)

    def test_does_not_fire_on_clean_exit(self):
        ev = HealthEvaluator(DetectionConfig())
        ev.evaluate(snap(state=ContainerState.RUNNING, t=0))
        triggered, _ = ev.evaluate(snap(state=ContainerState.EXITED, exit_code=0, t=5))
        assert "unexpected_stop" not in rule_ids(triggered)


class TestStoppedBeyondGrace:
    def test_does_not_fire_before_grace_period(self):
        cfg = DetectionConfig(stopped_grace_period_seconds=30)
        ev = HealthEvaluator(cfg)
        triggered, _ = ev.evaluate(snap(state=ContainerState.EXITED, t=0))
        assert "stopped_beyond_grace" not in rule_ids(triggered)

    def test_fires_after_grace_period_elapses(self):
        cfg = DetectionConfig(stopped_grace_period_seconds=30)
        ev = HealthEvaluator(cfg)
        ev.evaluate(snap(state=ContainerState.EXITED, t=0))
        triggered, _ = ev.evaluate(snap(state=ContainerState.EXITED, t=31))
        assert "stopped_beyond_grace" in rule_ids(triggered)

    def test_clears_once_running_again(self):
        cfg = DetectionConfig(stopped_grace_period_seconds=30)
        ev = HealthEvaluator(cfg)
        ev.evaluate(snap(state=ContainerState.EXITED, t=0))
        ev.evaluate(snap(state=ContainerState.EXITED, t=31))
        _, cleared = ev.evaluate(snap(state=ContainerState.RUNNING, t=35))
        assert "stopped_beyond_grace" in cleared


class TestRestartLoop:
    def test_fires_after_threshold_restarts_in_window(self):
        cfg = DetectionConfig(restart_count_threshold=3, restart_window_seconds=300)
        ev = HealthEvaluator(cfg)
        for i, count in enumerate([1, 2, 3]):
            triggered, _ = ev.evaluate(snap(restart_count=count, t=i * 10))
        assert "restart_loop" in rule_ids(triggered)

    def test_old_restarts_outside_window_do_not_count(self):
        cfg = DetectionConfig(restart_count_threshold=3, restart_window_seconds=60)
        ev = HealthEvaluator(cfg)
        ev.evaluate(snap(restart_count=1, t=0))
        ev.evaluate(snap(restart_count=2, t=10))
        # big gap -- first two restarts fall out of the window
        triggered, _ = ev.evaluate(snap(restart_count=3, t=200))
        assert "restart_loop" not in rule_ids(triggered)


class TestStartupHealthTimeout:
    def test_fires_when_stuck_starting_past_grace(self):
        cfg = DetectionConfig(startup_health_grace_seconds=60)
        ev = HealthEvaluator(cfg)
        triggered, _ = ev.evaluate(
            snap(health=HealthStatus.STARTING, started_at=BASE_TIME, t=61)
        )
        assert "startup_health_timeout" in rule_ids(triggered)

    def test_does_not_fire_within_grace(self):
        cfg = DetectionConfig(startup_health_grace_seconds=60)
        ev = HealthEvaluator(cfg)
        triggered, _ = ev.evaluate(
            snap(health=HealthStatus.STARTING, started_at=BASE_TIME, t=10)
        )
        assert "startup_health_timeout" not in rule_ids(triggered)


class TestSustainedResourceRules:
    def test_cpu_requires_sustained_breach(self):
        cfg = DetectionConfig(cpu_threshold_percent=80, sustained_duration_seconds=30)
        ev = HealthEvaluator(cfg)
        triggered, _ = ev.evaluate(snap(cpu=95, t=0))
        assert "cpu_sustained_high" not in rule_ids(triggered)
        triggered, _ = ev.evaluate(snap(cpu=95, t=31))
        assert "cpu_sustained_high" in rule_ids(triggered)

    def test_cpu_spike_then_drop_does_not_fire(self):
        cfg = DetectionConfig(cpu_threshold_percent=80, sustained_duration_seconds=30)
        ev = HealthEvaluator(cfg)
        ev.evaluate(snap(cpu=95, t=0))
        ev.evaluate(snap(cpu=10, t=10))
        triggered, _ = ev.evaluate(snap(cpu=95, t=40))
        assert "cpu_sustained_high" not in rule_ids(triggered)

    def test_memory_requires_sustained_breach(self):
        cfg = DetectionConfig(memory_threshold_percent=90, sustained_duration_seconds=30)
        ev = HealthEvaluator(cfg)
        ev.evaluate(snap(mem_percent=95, t=0))
        triggered, _ = ev.evaluate(snap(mem_percent=95, t=31))
        assert "memory_sustained_high" in rule_ids(triggered)


class TestMemoryGrowth:
    def test_fires_on_monotonic_increase(self):
        cfg = DetectionConfig(memory_growth_intervals=4)
        ev = HealthEvaluator(cfg)
        triggered = []
        for i, mem in enumerate([100, 150, 200, 250]):
            triggered, _ = ev.evaluate(snap(mem_bytes=mem, t=i * 15))
        assert "memory_growth" in rule_ids(triggered)

    def test_does_not_fire_when_memory_fluctuates(self):
        cfg = DetectionConfig(memory_growth_intervals=4)
        ev = HealthEvaluator(cfg)
        triggered = []
        for i, mem in enumerate([100, 150, 120, 250]):
            triggered, _ = ev.evaluate(snap(mem_bytes=mem, t=i * 15))
        assert "memory_growth" not in rule_ids(triggered)


class TestMetricsUnreliable:
    def test_fires_after_consecutive_unreliable_samples(self):
        cfg = DetectionConfig(collection_failure_threshold=3)
        ev = HealthEvaluator(cfg)
        triggered = []
        for i in range(3):
            triggered, _ = ev.evaluate(snap(reliable=False, t=i * 15))
        assert "metrics_unreliable" in rule_ids(triggered)

    def test_resets_on_a_reliable_sample(self):
        cfg = DetectionConfig(collection_failure_threshold=3)
        ev = HealthEvaluator(cfg)
        ev.evaluate(snap(reliable=False, t=0))
        ev.evaluate(snap(reliable=False, t=15))
        ev.evaluate(snap(reliable=True, t=30))
        triggered, _ = ev.evaluate(snap(reliable=False, t=45))
        assert "metrics_unreliable" not in rule_ids(triggered)


class TestExclusion:
    def test_excluded_container_never_triggers(self):
        ev = HealthEvaluator(DetectionConfig())
        triggered, _ = ev.evaluate(snap(health=HealthStatus.UNHEALTHY, excluded=True))
        assert triggered == []


class TestDaemonReachability:
    def test_fires_after_consecutive_failures(self):
        cfg = DetectionConfig(collection_failure_threshold=2)
        ev = HealthEvaluator(cfg)
        triggered, _ = ev.evaluate_daemon_reachability(False)
        assert triggered == []
        triggered, _ = ev.evaluate_daemon_reachability(False)
        assert len(triggered) == 1
        assert triggered[0].rule_id == "docker_daemon_unreachable"

    def test_recovers_resets_streak(self):
        cfg = DetectionConfig(collection_failure_threshold=2)
        ev = HealthEvaluator(cfg)
        ev.evaluate_daemon_reachability(False)
        _, cleared = ev.evaluate_daemon_reachability(True)
        assert "docker_daemon_unreachable" in cleared
        triggered, _ = ev.evaluate_daemon_reachability(False)
        assert triggered == []
