"""Docker SDK interactions tested via a mocked docker.DockerClient -- no
real daemon required. Complements the live-Docker verification already
done manually and in tests/e2e.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from docker.errors import APIError, DockerException, NotFound

from docker_health_guardian.collector.docker_client import DockerCollector, DockerUnavailableError
from docker_health_guardian.config.models import DockerConfig
from docker_health_guardian.models import ContainerState, HealthStatus


def make_fake_container(
    name="app",
    container_id="abc123",
    status="running",
    health=None,
    exit_code=0,
    restart_count=0,
    labels=None,
    started_at="2026-01-01T00:00:00.000000000Z",
    stats_payload=None,
    stats_error=None,
):
    container = MagicMock()
    container.name = name
    container.id = container_id
    state = {"Status": status, "ExitCode": exit_code, "StartedAt": started_at}
    if health:
        state["Health"] = {"Status": health}
    container.attrs = {
        "State": state,
        "Config": {"Image": "demo:latest", "Labels": labels or {}},
        "RestartCount": restart_count,
    }
    if stats_error:
        container.stats.side_effect = stats_error
    else:
        container.stats.return_value = stats_payload or {}
    return container


@pytest.fixture
def fake_client():
    return MagicMock()


class TestPing:
    def test_ping_true_when_daemon_reachable(self, fake_client):
        fake_client.ping.return_value = True
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector.ping() is True

    def test_ping_false_on_docker_exception(self, fake_client):
        fake_client.ping.side_effect = DockerException("down")
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector.ping() is False


class TestListContainerSnapshots:
    def test_raises_docker_unavailable_when_list_fails(self, fake_client):
        fake_client.containers.list.side_effect = DockerException("unreachable")
        collector = DockerCollector(DockerConfig(), client=fake_client)
        with pytest.raises(DockerUnavailableError):
            collector.list_container_snapshots()

    def test_skips_one_bad_container_but_returns_the_rest(self, fake_client):
        good = make_fake_container(name="good")
        bad = make_fake_container(name="bad")
        bad.attrs = property(lambda self: (_ for _ in ()).throw(APIError("boom")))
        type(bad).attrs = property(lambda self: (_ for _ in ()).throw(APIError("boom")))
        fake_client.containers.list.return_value = [good, bad]
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshots = collector.list_container_snapshots()
        assert len(snapshots) == 1
        assert snapshots[0].name == "good"

    def test_maps_state_and_health_correctly(self, fake_client):
        container = make_fake_container(status="running", health="healthy")
        fake_client.containers.list.return_value = [container]
        fake_client.info.return_value = {"MemTotal": 8_000_000_000}
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].state == ContainerState.RUNNING
        assert snapshots[0].health_status == HealthStatus.HEALTHY

    def test_no_healthcheck_configured_maps_to_none_status(self, fake_client):
        container = make_fake_container(status="running", health=None)
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].health_status == HealthStatus.NONE

    def test_never_started_container_has_no_started_at(self, fake_client):
        container = make_fake_container(
            status="created", started_at="0001-01-01T00:00:00Z"
        )
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].started_at is None

    def test_excluded_by_label_skips_stats_fetch(self, fake_client):
        container = make_fake_container(labels={"guardian.monitoring": "disabled"})
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].monitoring_excluded is True
        container.stats.assert_not_called()

    def test_excluded_by_config_list(self, fake_client):
        container = make_fake_container(name="sidecar")
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(
            DockerConfig(excluded_containers=["sidecar"]), client=fake_client
        )
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].monitoring_excluded is True

    def test_monitored_containers_allowlist_excludes_others(self, fake_client):
        container = make_fake_container(name="not-in-list")
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(
            DockerConfig(monitored_containers=["other-app"]), client=fake_client
        )
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].monitoring_excluded is True

    def test_stopped_container_does_not_fetch_stats(self, fake_client):
        container = make_fake_container(status="exited")
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(DockerConfig(), client=fake_client)
        collector.list_container_snapshots()
        container.stats.assert_not_called()

    def test_stats_fetch_failure_is_handled_gracefully(self, fake_client):
        container = make_fake_container(status="running", stats_error=APIError("boom"))
        fake_client.containers.list.return_value = [container]
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshots = collector.list_container_snapshots()
        assert snapshots[0].stats.reliable is False


class TestHostMemory:
    def test_caches_host_memory_after_first_lookup(self, fake_client):
        fake_client.info.return_value = {"MemTotal": 16_000_000_000}
        collector = DockerCollector(DockerConfig(), client=fake_client)
        collector._host_memory()
        collector._host_memory()
        assert fake_client.info.call_count == 1

    def test_handles_missing_mem_total_gracefully(self, fake_client):
        fake_client.info.return_value = {}
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector._host_memory() is None

    def test_handles_info_call_failure_gracefully(self, fake_client):
        fake_client.info.side_effect = DockerException("boom")
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector._host_memory() is None


class TestGetLogExcerpt:
    def test_returns_decoded_logs(self, fake_client):
        container = MagicMock()
        container.logs.return_value = b"line one\nline two\n"
        fake_client.containers.get.return_value = container
        collector = DockerCollector(DockerConfig(), client=fake_client)
        text = collector.get_log_excerpt("app")
        assert "line one" in text

    def test_missing_container_returns_empty_string(self, fake_client):
        fake_client.containers.get.side_effect = NotFound("nope")
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector.get_log_excerpt("app") == ""


class TestRestartContainer:
    def test_calls_restart_on_the_named_container(self, fake_client):
        container = MagicMock()
        fake_client.containers.get.return_value = container
        collector = DockerCollector(DockerConfig(), client=fake_client)
        collector.restart_container("app")
        container.restart.assert_called_once()


class TestGetSingleSnapshot:
    def test_returns_none_when_not_found(self, fake_client):
        fake_client.containers.get.side_effect = NotFound("nope")
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector.get_single_snapshot("app") is None

    def test_returns_snapshot_when_found(self, fake_client):
        container = make_fake_container(name="app")
        fake_client.containers.get.return_value = container
        collector = DockerCollector(DockerConfig(), client=fake_client)
        snapshot = collector.get_single_snapshot("app")
        assert snapshot is not None
        assert snapshot.name == "app"


class TestGetRecentEvents:
    def test_returns_list_of_events(self, fake_client):
        fake_client.events.return_value = iter([{"Type": "container"}, {"Type": "network"}])
        collector = DockerCollector(DockerConfig(), client=fake_client)
        events = collector.get_recent_events()
        assert len(events) == 2

    def test_handles_failure_gracefully(self, fake_client):
        fake_client.events.side_effect = DockerException("boom")
        collector = DockerCollector(DockerConfig(), client=fake_client)
        assert collector.get_recent_events() == []
