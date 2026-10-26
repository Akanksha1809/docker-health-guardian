"""FastAPI app exposing read-only health/incident/metrics endpoints.

Deliberately read-only and narrow: it never exposes arbitrary Docker
operations or a remote-control surface. Binds to 127.0.0.1 by default (see
ApiConfig.host) -- remote access requires the operator to explicitly change
the bind host and add their own authentication/network controls, documented
in docs/SECURITY.md.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from docker_health_guardian.config import GuardianAppConfig, load_config
from docker_health_guardian.persistence import Database

app = FastAPI(
    title="Docker Health Guardian API",
    description="Read-only health, incident, and metrics endpoints.",
    version="0.1.0",
)

_state: dict = {}


def configure(config_path: str | None = None) -> None:
    cfg: GuardianAppConfig = load_config(config_path)
    db = Database(cfg.persistence.db_path)
    db.initialize()
    _state["config"] = cfg
    _state["db"] = db


def _db() -> Database:
    if "db" not in _state:
        configure()
    return _state["db"]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/ready")
def ready() -> dict:
    try:
        db = _db()
        db.count_incidents()
    except Exception as exc:  # noqa: BLE001 -- readiness probe must report, not 500 blindly
        raise HTTPException(status_code=503, detail=f"not ready: {exc}") from exc
    return {"status": "ready"}


@app.get("/api/v1/containers")
def list_containers() -> list[dict]:
    from docker_health_guardian.collector import DockerCollector, DockerUnavailableError

    cfg = _state.get("config") or load_config()
    try:
        collector = DockerCollector(cfg.docker)
        snapshots = collector.list_container_snapshots()
    except DockerUnavailableError as exc:
        raise HTTPException(status_code=503, detail=f"docker unreachable: {exc}") from exc
    return [s.model_dump(mode="json") for s in snapshots]


@app.get("/api/v1/incidents")
def list_incidents(
    state: str | None = None,
    severity: str | None = None,
    container_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    db = _db()
    incidents = db.list_incidents(
        state=state, severity=severity, container_name=container_name, limit=limit, offset=offset
    )
    total = db.count_incidents(state=state, severity=severity)
    return {
        "items": [i.model_dump(mode="json") for i in incidents],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.get("/api/v1/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict:
    db = _db()
    incident = db.get_incident(incident_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} not found")
    events = db.list_events(incident_id)
    attempts = db.list_remediation_attempts(incident_id=incident_id)
    return {
        "incident": incident.model_dump(mode="json"),
        "events": [e.model_dump(mode="json") for e in events],
        "remediation_attempts": [a.model_dump(mode="json") for a in attempts],
    }


@app.get("/api/v1/remediation/history")
def remediation_history(
    container_name: str | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict:
    db = _db()
    attempts = db.list_remediation_attempts(
        container_name=container_name, limit=limit, offset=offset
    )
    return {
        "items": [a.model_dump(mode="json") for a in attempts],
        "limit": limit,
        "offset": offset,
    }


@app.get("/metrics", response_class=PlainTextResponse)
def metrics() -> str:
    from prometheus_client import generate_latest

    from docker_health_guardian.metrics import sync_metrics_from_db

    sync_metrics_from_db(_db())
    return generate_latest().decode("utf-8")
