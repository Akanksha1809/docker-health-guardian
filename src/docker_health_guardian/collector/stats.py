"""Pure CPU/memory calculations from Docker's raw `/containers/{id}/stats` payloads.

Kept free of any Docker SDK/network dependency so these calculations can be
unit-tested with synthetic payloads (see tests/unit/test_stats.py).

CPU formula follows Docker's own documented approach (as used by the `docker
stats` CLI):

    cpu_delta    = cpu_stats.cpu_usage.total_usage - precpu_stats.cpu_usage.total_usage
    system_delta = cpu_stats.system_cpu_usage      - precpu_stats.system_cpu_usage
    cpu_percent  = (cpu_delta / system_delta) * online_cpus * 100

This requires two samples (current + previous); Docker's `stats(stream=False)`
single-shot call already returns both `cpu_stats` and `precpu_stats` from one
API call, so no separate "previous sample" bookkeeping is needed by callers.

Memory assumption (documented explicitly per project requirements): Docker's
`memory_stats.usage` on cgroups includes reclaimable page cache. We subtract
the cache figure (`memory_stats.stats.cache` on cgroup v1, or
`memory_stats.stats.inactive_file` on cgroup v2, whichever is present) to
approximate the container's "real" working-set memory, matching the behavior
of `docker stats`. A memory limit is only considered "configured" (and thus a
meaningful percentage) when it is strictly less than the host's total memory;
Docker reports the host's total memory as the limit when no `--memory` flag
was set, and treating that as 100%-of-host would be misleading.
"""

from __future__ import annotations

from docker_health_guardian.models import ResourceStats

# Docker reports the host's total memory as the container limit when no
# explicit --memory constraint was set. We only treat a limit as "configured"
# below this fraction of host total memory.
_UNLIMITED_MEMORY_FRACTION = 0.95


def compute_cpu_percent(stats: dict) -> tuple[float | None, str | None]:
    """Returns (cpu_percent, unavailable_reason)."""
    try:
        cpu_stats = stats["cpu_stats"]
        precpu_stats = stats["precpu_stats"]
        cpu_usage = cpu_stats["cpu_usage"]["total_usage"]
        precpu_usage = precpu_stats["cpu_usage"]["total_usage"]
        system_usage = cpu_stats["system_cpu_usage"]
        presystem_usage = precpu_stats["system_cpu_usage"]
    except (KeyError, TypeError):
        return None, "missing cpu stats fields"

    cpu_delta = cpu_usage - precpu_usage
    system_delta = system_usage - presystem_usage

    if system_delta <= 0:
        # First sample after container start, or no CPU activity on the host
        # delta window yet -- not an error, just not computable this cycle.
        return None, "no system cpu delta available yet"
    if cpu_delta < 0:
        return None, "negative cpu delta (counter reset)"

    online_cpus = cpu_stats.get("online_cpus")
    if not online_cpus:
        percpu = cpu_stats.get("cpu_usage", {}).get("percpu_usage")
        online_cpus = len(percpu) if percpu else 1

    cpu_percent = (cpu_delta / system_delta) * online_cpus * 100.0
    return round(cpu_percent, 2), None


def compute_memory_stats(
    stats: dict, host_total_memory_bytes: int | None
) -> tuple[int | None, int | None, float | None, str | None]:
    """Returns (usage_bytes, limit_bytes, percent, unavailable_reason)."""
    try:
        memory_stats = stats["memory_stats"]
        usage = memory_stats["usage"]
        limit = memory_stats["limit"]
    except (KeyError, TypeError):
        return None, None, None, "missing memory stats fields"

    detail = memory_stats.get("stats", {}) or {}
    cache = detail.get("cache", detail.get("inactive_file", 0)) or 0
    real_usage = usage - cache if usage > cache else usage

    if limit <= 0:
        return real_usage, None, None, "no memory limit reported"

    is_unlimited = (
        host_total_memory_bytes is not None
        and limit >= host_total_memory_bytes * _UNLIMITED_MEMORY_FRACTION
    )
    if is_unlimited:
        return real_usage, None, None, "no explicit memory limit configured"

    percent = round((real_usage / limit) * 100.0, 2) if limit else None
    return real_usage, limit, percent, None


def build_resource_stats(
    raw_stats: dict | None, host_total_memory_bytes: int | None
) -> ResourceStats:
    if raw_stats is None:
        return ResourceStats(reliable=False, unavailable_reason="no stats payload returned")

    cpu_percent, cpu_reason = compute_cpu_percent(raw_stats)
    mem_usage, mem_limit, mem_percent, mem_reason = compute_memory_stats(
        raw_stats, host_total_memory_bytes
    )

    networks = raw_stats.get("networks") or {}
    rx_bytes = sum(n.get("rx_bytes", 0) for n in networks.values()) if networks else None
    tx_bytes = sum(n.get("tx_bytes", 0) for n in networks.values()) if networks else None

    blkio = raw_stats.get("blkio_stats", {}).get("io_service_bytes_recursive") or []
    read_bytes = sum(e["value"] for e in blkio if e.get("op") == "Read") if blkio else None
    write_bytes = sum(e["value"] for e in blkio if e.get("op") == "Write") if blkio else None

    reliable = cpu_reason is None
    unavailable_reason = cpu_reason or mem_reason

    return ResourceStats(
        cpu_percent=cpu_percent,
        memory_usage_bytes=mem_usage,
        memory_limit_bytes=mem_limit,
        memory_percent=mem_percent,
        network_rx_bytes=rx_bytes,
        network_tx_bytes=tx_bytes,
        block_read_bytes=read_bytes,
        block_write_bytes=write_bytes,
        reliable=reliable,
        unavailable_reason=unavailable_reason,
    )
