from docker_health_guardian.collector.stats import (
    build_resource_stats,
    compute_cpu_percent,
    compute_memory_stats,
)

GB = 1024**3


def _cpu_payload(cpu_delta: int, system_delta: int, online_cpus: int = 4) -> dict:
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000 + cpu_delta},
            "system_cpu_usage": 10_000_000 + system_delta,
            "online_cpus": online_cpus,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": 1_000_000},
            "system_cpu_usage": 10_000_000,
        },
    }


class TestComputeCpuPercent:
    def test_normal_delta_computes_expected_percent(self):
        payload = _cpu_payload(cpu_delta=200_000, system_delta=2_000_000, online_cpus=4)
        percent, reason = compute_cpu_percent(payload)
        assert reason is None
        assert percent == (200_000 / 2_000_000) * 4 * 100.0

    def test_zero_system_delta_is_unavailable_not_zero(self):
        payload = _cpu_payload(cpu_delta=0, system_delta=0)
        percent, reason = compute_cpu_percent(payload)
        assert percent is None
        assert reason is not None

    def test_missing_fields_is_unavailable(self):
        percent, reason = compute_cpu_percent({"cpu_stats": {}})
        assert percent is None
        assert reason is not None

    def test_negative_cpu_delta_counter_reset_is_unavailable(self):
        payload = _cpu_payload(cpu_delta=-500, system_delta=2_000_000)
        percent, reason = compute_cpu_percent(payload)
        assert percent is None
        assert "reset" in reason

    def test_falls_back_to_percpu_length_when_online_cpus_missing(self):
        payload = _cpu_payload(cpu_delta=100_000, system_delta=1_000_000, online_cpus=0)
        payload["cpu_stats"]["cpu_usage"]["percpu_usage"] = [1, 2, 3]
        percent, reason = compute_cpu_percent(payload)
        assert reason is None
        assert percent == round((100_000 / 1_000_000) * 3 * 100.0, 2)


class TestComputeMemoryStats:
    def test_configured_limit_below_host_total_yields_percent(self):
        payload = {
            "memory_stats": {
                "usage": int(0.5 * GB),
                "limit": 1 * GB,
                "stats": {"cache": 0},
            }
        }
        usage, limit, percent, reason = compute_memory_stats(payload, host_total_memory_bytes=8 * GB)
        assert reason is None
        assert usage == int(0.5 * GB)
        assert limit == 1 * GB
        assert percent == 50.0

    def test_cache_is_subtracted_from_usage_cgroup_v1(self):
        payload = {
            "memory_stats": {
                "usage": int(0.6 * GB),
                "limit": 1 * GB,
                "stats": {"cache": int(0.1 * GB)},
            }
        }
        usage, _, percent, _ = compute_memory_stats(payload, host_total_memory_bytes=8 * GB)
        assert usage == int(0.5 * GB)
        assert percent == 50.0

    def test_inactive_file_is_subtracted_cgroup_v2(self):
        payload = {
            "memory_stats": {
                "usage": int(0.6 * GB),
                "limit": 1 * GB,
                "stats": {"inactive_file": int(0.1 * GB)},
            }
        }
        usage, _, _, _ = compute_memory_stats(payload, host_total_memory_bytes=8 * GB)
        assert usage == int(0.5 * GB)

    def test_limit_near_host_total_treated_as_unlimited(self):
        payload = {
            "memory_stats": {
                "usage": int(1 * GB),
                "limit": int(7.9 * GB),
                "stats": {},
            }
        }
        usage, limit, percent, reason = compute_memory_stats(payload, host_total_memory_bytes=8 * GB)
        assert limit is None
        assert percent is None
        assert reason == "no explicit memory limit configured"
        assert usage == 1 * GB

    def test_zero_limit_is_unavailable(self):
        payload = {"memory_stats": {"usage": 100, "limit": 0, "stats": {}}}
        usage, limit, percent, reason = compute_memory_stats(payload, host_total_memory_bytes=8 * GB)
        assert limit is None
        assert percent is None
        assert reason == "no memory limit reported"

    def test_missing_fields_is_unavailable(self):
        usage, limit, percent, reason = compute_memory_stats({}, host_total_memory_bytes=8 * GB)
        assert usage is None and limit is None and percent is None
        assert reason is not None


class TestBuildResourceStats:
    def test_none_payload_marks_unreliable(self):
        stats = build_resource_stats(None, host_total_memory_bytes=8 * GB)
        assert stats.reliable is False
        assert stats.cpu_percent is None

    def test_full_payload_computes_network_and_blkio_totals(self):
        payload = _cpu_payload(cpu_delta=100_000, system_delta=1_000_000)
        payload["memory_stats"] = {"usage": int(0.2 * GB), "limit": 1 * GB, "stats": {"cache": 0}}
        payload["networks"] = {
            "eth0": {"rx_bytes": 1000, "tx_bytes": 2000},
            "eth1": {"rx_bytes": 500, "tx_bytes": 250},
        }
        payload["blkio_stats"] = {
            "io_service_bytes_recursive": [
                {"op": "Read", "value": 4096},
                {"op": "Write", "value": 2048},
                {"op": "Read", "value": 1024},
            ]
        }
        stats = build_resource_stats(payload, host_total_memory_bytes=8 * GB)
        assert stats.reliable is True
        assert stats.network_rx_bytes == 1500
        assert stats.network_tx_bytes == 2250
        assert stats.block_read_bytes == 5120
        assert stats.block_write_bytes == 2048
