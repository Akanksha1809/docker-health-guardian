"""Report generation: JSON, CSV, and a human-readable HTML summary.

Every report explicitly separates observed facts (incident state/metrics),
probable diagnosis (with its own confidence label), attempted actions, and
verified outcomes -- never collapsing "we restarted it" into "it's fixed".
"""

from __future__ import annotations

import csv
import io
import json
from html import escape

from docker_health_guardian.persistence import Database

SUPPORTED_FORMATS = ("json", "csv", "html")


def _collect(db: Database, limit: int = 200) -> list[dict]:
    incidents = db.list_incidents(limit=limit)
    rows = []
    for incident in incidents:
        attempts = db.list_remediation_attempts(incident_id=incident.incident_id)
        rows.append(
            {
                "incident_id": incident.incident_id,
                "container_name": incident.container_name,
                "rule_id": incident.rule_id,
                "severity": incident.severity.value,
                "state": incident.state.value,
                "first_seen": incident.first_seen.isoformat(),
                "last_seen": incident.last_seen.isoformat(),
                "resolved_at": incident.resolved_at.isoformat() if incident.resolved_at else "",
                "occurrence_count": incident.occurrence_count,
                "diagnosis_likely_cause": (
                    incident.diagnosis.likely_cause if incident.diagnosis else ""
                ),
                "diagnosis_confidence": (
                    incident.diagnosis.confidence.value if incident.diagnosis else ""
                ),
                "remediation_attempts": len(attempts),
                "remediation_verified_success": sum(
                    1 for a in attempts if a.recovery_verified is True
                ),
            }
        )
    return rows


def _to_json(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2, default=str)


def _to_csv(rows: list[dict]) -> str:
    if not rows:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


def _to_html(rows: list[dict]) -> str:
    if not rows:
        return "<html><body><p>No incidents recorded.</p></body></html>"
    headers = "".join(f"<th>{escape(k)}</th>" for k in rows[0].keys())
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{escape(str(v))}</td>" for v in row.values())
        body_rows.append(f"<tr>{cells}</tr>")
    return (
        "<html><head><title>Docker Health Guardian Report</title>"
        "<style>table{border-collapse:collapse}td,th{border:1px solid #ccc;"
        "padding:4px 8px;font-family:monospace;font-size:13px}</style></head>"
        f"<body><h1>Docker Health Guardian Report</h1><table><tr>{headers}</tr>"
        f"{''.join(body_rows)}</table></body></html>"
    )


def generate_report(db: Database, output_format: str = "json", limit: int = 200) -> str:
    if output_format not in SUPPORTED_FORMATS:
        raise ValueError(
            f"unsupported report format: {output_format!r}, use one of {SUPPORTED_FORMATS}"
        )
    rows = _collect(db, limit=limit)
    if output_format == "json":
        return _to_json(rows)
    if output_format == "csv":
        return _to_csv(rows)
    return _to_html(rows)
