"""Post-action recovery verification.

A successful `docker restart` command is not proof of recovery -- this polls
the container's actual state/health until it looks genuinely recovered or a
bounded timeout elapses. `clock`/`sleep` are injectable so tests can run
instantly instead of waiting on real time.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from docker_health_guardian.collector import DockerCollector
from docker_health_guardian.models import ContainerState, HealthStatus

_ACCEPTABLE_HEALTH = (HealthStatus.HEALTHY, HealthStatus.NONE)


def verify_recovery(
    collector: DockerCollector,
    container_name: str,
    timeout_seconds: float,
    poll_interval_seconds: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[bool, str]:
    deadline = clock() + timeout_seconds
    last_detail = "container was never observed during the verification window"

    while True:
        snapshot = collector.get_single_snapshot(container_name)
        if snapshot is None:
            last_detail = "container not found"
        elif (
            snapshot.state == ContainerState.RUNNING
            and snapshot.health_status in _ACCEPTABLE_HEALTH
        ):
            return True, f"container running (health={snapshot.health_status.value})"
        else:
            last_detail = f"state={snapshot.state.value} health={snapshot.health_status.value}"

        if clock() >= deadline:
            break
        sleep(min(poll_interval_seconds, max(deadline - clock(), 0)))

    return False, f"not recovered within {timeout_seconds}s: {last_detail}"
