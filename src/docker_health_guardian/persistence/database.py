"""SQLite persistence layer.

Design notes:
- A short-lived connection is opened per operation (via `_connect`), not held
  for the service's lifetime, so the scheduler thread, CLI, and API process
  can all safely use their own `Database` instance against the same file.
- WAL journal mode is enabled so readers (CLI/API) don't block the writer
  (scheduler) and vice versa.
- All timestamps are stored as ISO-8601 UTC strings.
- Schema changes go through `_MIGRATIONS`, applied in order based on the
  `schema_version` table -- a simple, auditable migration mechanism without
  pulling in a full migration framework for a single-file SQLite database.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from docker_health_guardian.models import (
    Incident,
    IncidentEvent,
    IncidentState,
    NotificationOutcome,
    NotificationRecord,
    RemediationAttempt,
    RemediationOutcome,
    Severity,
)

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS incidents (
    incident_id TEXT PRIMARY KEY,
    container_name TEXT NOT NULL,
    container_id TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    incident_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    state TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    resolved_at TEXT,
    observed_metrics TEXT NOT NULL DEFAULT '{}',
    diagnosis TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    occurrence_count INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_incidents_container ON incidents(container_name);
CREATE INDEX IF NOT EXISTS idx_incidents_state ON incidents(state);
CREATE INDEX IF NOT EXISTS idx_incidents_rule ON incidents(container_name, rule_id);

CREATE TABLE IF NOT EXISTS incident_events (
    event_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    timestamp TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_incident ON incident_events(incident_id);

CREATE TABLE IF NOT EXISTS remediation_attempts (
    attempt_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    container_name TEXT NOT NULL,
    action TEXT NOT NULL,
    dry_run INTEGER NOT NULL,
    reason TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT,
    recovery_verified INTEGER
);
CREATE INDEX IF NOT EXISTS idx_remediation_container ON remediation_attempts(container_name);
CREATE INDEX IF NOT EXISTS idx_remediation_incident ON remediation_attempts(incident_id);

CREATE TABLE IF NOT EXISTS notifications (
    record_id TEXT PRIMARY KEY,
    incident_id TEXT NOT NULL REFERENCES incidents(incident_id),
    channel TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    attempted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notifications_incident ON notifications(incident_id);

-- An explicit per-container override set via `guardian remediation enable/disable`.
-- NULL/absent means "use the static restart_allowlist from guardian.yaml".
-- This can only ever make a container MORE restricted or explicitly eligible
-- within the allowlist model -- it never bypasses protected_containers.
CREATE TABLE IF NOT EXISTS container_overrides (
    container_name TEXT PRIMARY KEY,
    remediation_enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS: list[str] = [_SCHEMA_V1]


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        path = Path(self._db_path)
        if path.parent and str(path.parent) not in ("", "."):
            path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            current_version = 0
            try:
                row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
                current_version = row[0] or 0
            except sqlite3.OperationalError:
                current_version = 0
            for version, script in enumerate(_MIGRATIONS, start=1):
                if version > current_version:
                    conn.executescript(script)
                    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))

    # -- incidents -----------------------------------------------------

    def upsert_incident(self, incident: Incident) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO incidents (
                    incident_id, container_name, container_id, rule_id, incident_type,
                    severity, state, first_seen, last_seen, resolved_at, observed_metrics,
                    diagnosis, notified, occurrence_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(incident_id) DO UPDATE SET
                    state=excluded.state,
                    last_seen=excluded.last_seen,
                    resolved_at=excluded.resolved_at,
                    observed_metrics=excluded.observed_metrics,
                    diagnosis=excluded.diagnosis,
                    notified=excluded.notified,
                    occurrence_count=excluded.occurrence_count
                """,
                (
                    incident.incident_id,
                    incident.container_name,
                    incident.container_id,
                    incident.rule_id,
                    incident.incident_type,
                    incident.severity.value,
                    incident.state.value,
                    _dt(incident.first_seen),
                    _dt(incident.last_seen),
                    _dt(incident.resolved_at),
                    json.dumps(incident.observed_metrics, default=str),
                    incident.diagnosis.model_dump_json() if incident.diagnosis else None,
                    int(incident.notified),
                    incident.occurrence_count,
                ),
            )

    def get_incident(self, incident_id: str) -> Incident | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)
            ).fetchone()
            return self._row_to_incident(row) if row else None

    def find_open_incident(self, container_name: str, rule_id: str) -> Incident | None:
        open_states = [s.value for s in IncidentState if s not in (
            IncidentState.RESOLVED, IncidentState.SUPPRESSED
        )]
        placeholders = ",".join("?" for _ in open_states)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM incidents
                WHERE container_name = ? AND rule_id = ? AND state IN ({placeholders})
                ORDER BY last_seen DESC LIMIT 1
                """,
                (container_name, rule_id, *open_states),
            ).fetchone()
            return self._row_to_incident(row) if row else None

    def find_last_resolved(self, container_name: str, rule_id: str) -> Incident | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM incidents
                WHERE container_name = ? AND rule_id = ? AND state = ?
                ORDER BY resolved_at DESC LIMIT 1
                """,
                (container_name, rule_id, IncidentState.RESOLVED.value),
            ).fetchone()
            return self._row_to_incident(row) if row else None

    def list_incidents(
        self,
        state: str | None = None,
        severity: str | None = None,
        container_name: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Incident]:
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if container_name:
            clauses.append("container_name = ?")
            params.append(container_name)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM incidents {where} ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [self._row_to_incident(r) for r in rows]

    def count_incidents(
        self, state: str | None = None, severity: str | None = None
    ) -> int:
        clauses, params = [], []
        if state:
            clauses.append("state = ?")
            params.append(state)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            row = conn.execute(f"SELECT COUNT(*) FROM incidents {where}", params).fetchone()
            return row[0]

    @staticmethod
    def _row_to_incident(row: sqlite3.Row) -> Incident:
        from docker_health_guardian.models import Diagnosis

        diagnosis = Diagnosis.model_validate_json(row["diagnosis"]) if row["diagnosis"] else None
        return Incident(
            incident_id=row["incident_id"],
            container_name=row["container_name"],
            container_id=row["container_id"],
            rule_id=row["rule_id"],
            incident_type=row["incident_type"],
            severity=Severity(row["severity"]),
            state=IncidentState(row["state"]),
            first_seen=_parse_dt(row["first_seen"]),
            last_seen=_parse_dt(row["last_seen"]),
            resolved_at=_parse_dt(row["resolved_at"]),
            observed_metrics=json.loads(row["observed_metrics"]),
            diagnosis=diagnosis,
            notified=bool(row["notified"]),
            occurrence_count=row["occurrence_count"],
        )

    # -- incident events -------------------------------------------------

    def add_event(self, event: IncidentEvent) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO incident_events (event_id, incident_id, timestamp, kind, detail, data)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.incident_id,
                    _dt(event.timestamp),
                    event.kind,
                    event.detail,
                    json.dumps(event.data, default=str),
                ),
            )

    def list_events(self, incident_id: str) -> list[IncidentEvent]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM incident_events WHERE incident_id = ? ORDER BY timestamp ASC",
                (incident_id,),
            ).fetchall()
            return [
                IncidentEvent(
                    event_id=r["event_id"],
                    incident_id=r["incident_id"],
                    timestamp=_parse_dt(r["timestamp"]),
                    kind=r["kind"],
                    detail=r["detail"],
                    data=json.loads(r["data"]),
                )
                for r in rows
            ]

    # -- remediation attempts --------------------------------------------

    def upsert_remediation_attempt(self, attempt: RemediationAttempt) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO remediation_attempts (
                    attempt_id, incident_id, container_name, action, dry_run, reason,
                    outcome, detail, started_at, finished_at, recovery_verified
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    outcome=excluded.outcome,
                    detail=excluded.detail,
                    finished_at=excluded.finished_at,
                    recovery_verified=excluded.recovery_verified
                """,
                (
                    attempt.attempt_id,
                    attempt.incident_id,
                    attempt.container_name,
                    attempt.action,
                    int(attempt.dry_run),
                    attempt.reason,
                    attempt.outcome.value,
                    attempt.detail,
                    _dt(attempt.started_at),
                    _dt(attempt.finished_at),
                    None if attempt.recovery_verified is None else int(attempt.recovery_verified),
                ),
            )

    def count_attempts_for_incident(self, incident_id: str, dry_run: bool = False) -> int:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM remediation_attempts "
                "WHERE incident_id = ? AND dry_run = ?",
                (incident_id, int(dry_run)),
            ).fetchone()
            return row[0]

    def list_remediation_attempts(
        self,
        container_name: str | None = None,
        incident_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RemediationAttempt]:
        clauses, params = [], []
        if container_name:
            clauses.append("container_name = ?")
            params.append(container_name)
        if incident_id:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM remediation_attempts {where} "
                "ORDER BY started_at DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            ).fetchall()
            return [
                RemediationAttempt(
                    attempt_id=r["attempt_id"],
                    incident_id=r["incident_id"],
                    container_name=r["container_name"],
                    action=r["action"],
                    dry_run=bool(r["dry_run"]),
                    reason=r["reason"],
                    outcome=RemediationOutcome(r["outcome"]),
                    detail=r["detail"],
                    started_at=_parse_dt(r["started_at"]),
                    finished_at=_parse_dt(r["finished_at"]),
                    recovery_verified=(
                        None if r["recovery_verified"] is None else bool(r["recovery_verified"])
                    ),
                )
                for r in rows
            ]

    # -- notifications ----------------------------------------------------

    def add_notification_record(self, record: NotificationRecord) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO notifications
                    (record_id, incident_id, channel, outcome, detail, attempted_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id,
                    record.incident_id,
                    record.channel,
                    record.outcome.value,
                    record.detail,
                    _dt(record.attempted_at),
                ),
            )

    def list_notification_records(
        self, incident_id: str | None = None, limit: int = 50
    ) -> list[NotificationRecord]:
        where = "WHERE incident_id = ?" if incident_id else ""
        params = (incident_id,) if incident_id else ()
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM notifications {where} ORDER BY attempted_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
            return [
                NotificationRecord(
                    record_id=r["record_id"],
                    incident_id=r["incident_id"],
                    channel=r["channel"],
                    outcome=NotificationOutcome(r["outcome"]),
                    detail=r["detail"],
                    attempted_at=_parse_dt(r["attempted_at"]),
                )
                for r in rows
            ]

    # -- per-container remediation overrides -------------------------------

    def set_container_override(self, container_name: str, enabled: bool) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO container_overrides (container_name, remediation_enabled, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(container_name) DO UPDATE SET
                    remediation_enabled=excluded.remediation_enabled,
                    updated_at=excluded.updated_at
                """,
                (container_name, int(enabled), datetime.now(UTC).isoformat()),
            )

    def clear_container_override(self, container_name: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM container_overrides WHERE container_name = ?", (container_name,)
            )

    def get_container_override(self, container_name: str) -> bool | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT remediation_enabled FROM container_overrides WHERE container_name = ?",
                (container_name,),
            ).fetchone()
            return None if row is None else bool(row["remediation_enabled"])

    def list_container_overrides(self) -> dict[str, bool]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT container_name, remediation_enabled FROM container_overrides"
            ).fetchall()
            return {r["container_name"]: bool(r["remediation_enabled"]) for r in rows}

    # -- retention --------------------------------------------------------

    def cleanup_retention(self, retention_days: int) -> int:
        """Deletes RESOLVED/SUPPRESSED incidents (and their events/attempts/
        notifications) older than retention_days. Never touches open incidents.
        Returns the number of incidents removed."""
        # Compare as ISO-8601 strings with a consistent fixed UTC offset
        # (same format `_dt()` writes), which sorts lexicographically.
        cutoff_iso = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT incident_id FROM incidents
                WHERE state IN (?, ?) AND resolved_at IS NOT NULL
                AND resolved_at < ?
                """,
                (
                    IncidentState.RESOLVED.value,
                    IncidentState.SUPPRESSED.value,
                    cutoff_iso,
                ),
            ).fetchall()
            ids = [r["incident_id"] for r in rows]
            for incident_id in ids:
                conn.execute("DELETE FROM notifications WHERE incident_id = ?", (incident_id,))
                conn.execute(
                    "DELETE FROM remediation_attempts WHERE incident_id = ?", (incident_id,)
                )
                conn.execute("DELETE FROM incident_events WHERE incident_id = ?", (incident_id,))
                conn.execute("DELETE FROM incidents WHERE incident_id = ?", (incident_id,))
            return len(ids)
