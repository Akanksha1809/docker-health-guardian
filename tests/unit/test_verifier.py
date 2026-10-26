from docker_health_guardian.models import ContainerSnapshot, ContainerState, HealthStatus
from docker_health_guardian.remediation.verifier import verify_recovery


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeCollector:
    def __init__(self, snapshots_by_call: list):
        self._snapshots = list(snapshots_by_call)
        self.calls = 0

    def get_single_snapshot(self, container_name: str):
        snap = self._snapshots[min(self.calls, len(self._snapshots) - 1)]
        self.calls += 1
        return snap


def make_snapshot(state, health=HealthStatus.NONE):
    return ContainerSnapshot(
        container_id="id",
        name="app",
        image="demo",
        state=state,
        health_status=health,
    )


class TestVerifyRecovery:
    def test_succeeds_immediately_when_running_and_healthy(self):
        collector = FakeCollector([make_snapshot(ContainerState.RUNNING, HealthStatus.HEALTHY)])
        clock = FakeClock()
        ok, detail = verify_recovery(
            collector, "app", timeout_seconds=20, clock=clock, sleep=clock.sleep
        )
        assert ok is True
        assert "running" in detail

    def test_succeeds_when_running_with_no_healthcheck_configured(self):
        collector = FakeCollector([make_snapshot(ContainerState.RUNNING, HealthStatus.NONE)])
        clock = FakeClock()
        ok, _ = verify_recovery(
            collector, "app", timeout_seconds=20, clock=clock, sleep=clock.sleep
        )
        assert ok is True

    def test_fails_after_timeout_when_still_restarting(self):
        collector = FakeCollector([make_snapshot(ContainerState.RESTARTING)])
        clock = FakeClock()
        ok, detail = verify_recovery(
            collector,
            "app",
            timeout_seconds=10,
            poll_interval_seconds=2,
            clock=clock,
            sleep=clock.sleep,
        )
        assert ok is False
        assert "not recovered" in detail

    def test_recovers_on_a_later_poll(self):
        collector = FakeCollector(
            [
                make_snapshot(ContainerState.RESTARTING),
                make_snapshot(ContainerState.RESTARTING),
                make_snapshot(ContainerState.RUNNING, HealthStatus.HEALTHY),
            ]
        )
        clock = FakeClock()
        ok, _ = verify_recovery(
            collector,
            "app",
            timeout_seconds=20,
            poll_interval_seconds=2,
            clock=clock,
            sleep=clock.sleep,
        )
        assert ok is True
        assert collector.calls == 3

    def test_container_not_found_is_not_recovered(self):
        collector = FakeCollector([None])
        clock = FakeClock()
        ok, detail = verify_recovery(
            collector, "app", timeout_seconds=5, clock=clock, sleep=clock.sleep
        )
        assert ok is False
        assert "not found" in detail
