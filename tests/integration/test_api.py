import importlib

import pytest
from fastapi.testclient import TestClient

from docker_health_guardian.models import Incident, Severity
from docker_health_guardian.persistence import Database

# `docker_health_guardian.api`'s __init__ does `from .app import app`, which
# rebinds the package's `app` attribute from the submodule to the FastAPI
# instance (same name for the file and the exported symbol). importlib looks
# the submodule up directly in sys.modules, bypassing that shadowing, so we
# can still reach its module-level `_state` dict for test setup.
app_module = importlib.import_module("docker_health_guardian.api.app")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "guardian.db"
    db = Database(str(db_path))
    db.initialize()
    app_module._state["db"] = db
    app_module._state["config"] = None
    yield TestClient(app_module.app), db
    app_module._state.clear()


def make_incident(**overrides) -> Incident:
    defaults = dict(
        container_name="app",
        container_id="id-app",
        rule_id="health_unhealthy",
        incident_type="health_unhealthy",
        severity=Severity.HIGH,
    )
    defaults.update(overrides)
    return Incident(**defaults)


class TestHealthAndReady:
    def test_health_always_ok(self, client):
        test_client, _ = client
        resp = test_client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}

    def test_ready_when_db_initialized(self, client):
        test_client, _ = client
        resp = test_client.get("/ready")
        assert resp.status_code == 200


class TestIncidentEndpoints:
    def test_list_incidents_empty(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/incidents")
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0

    def test_list_incidents_returns_created_incident(self, client):
        test_client, db = client
        db.upsert_incident(make_incident())
        resp = test_client.get("/api/v1/incidents")
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["container_name"] == "app"

    def test_list_incidents_filters_by_severity(self, client):
        test_client, db = client
        db.upsert_incident(make_incident(severity=Severity.LOW))
        db.upsert_incident(make_incident(container_name="app2", severity=Severity.CRITICAL))
        resp = test_client.get("/api/v1/incidents", params={"severity": "critical"})
        body = resp.json()
        assert body["total"] == 1
        assert body["items"][0]["container_name"] == "app2"

    def test_get_incident_detail(self, client):
        test_client, db = client
        incident = make_incident()
        db.upsert_incident(incident)
        resp = test_client.get(f"/api/v1/incidents/{incident.incident_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["incident"]["incident_id"] == incident.incident_id
        assert "events" in body
        assert "remediation_attempts" in body

    def test_get_incident_404_when_missing(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/incidents/does-not-exist")
        assert resp.status_code == 404

    def test_pagination_limit_and_offset_respected(self, client):
        test_client, db = client
        for i in range(5):
            db.upsert_incident(make_incident(container_name=f"app{i}"))
        resp = test_client.get("/api/v1/incidents", params={"limit": 2, "offset": 0})
        body = resp.json()
        assert len(body["items"]) == 2
        assert body["total"] == 5


class TestRemediationHistoryEndpoint:
    def test_empty_history(self, client):
        test_client, _ = client
        resp = test_client.get("/api/v1/remediation/history")
        assert resp.status_code == 200
        assert resp.json()["items"] == []


class TestMetricsEndpoint:
    def test_metrics_endpoint_is_prometheus_text_format(self, client):
        test_client, _ = client
        resp = test_client.get("/metrics")
        assert resp.status_code == 200
        assert "guardian_open_incidents" in resp.text

    def test_metrics_reflect_incident_counts(self, client):
        test_client, db = client
        db.upsert_incident(make_incident(severity=Severity.CRITICAL))
        resp = test_client.get("/metrics")
        assert 'guardian_incidents_by_severity{severity="critical"} 1.0' in resp.text
