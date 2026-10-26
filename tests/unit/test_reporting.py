import json

import pytest

from docker_health_guardian.models import Incident, Severity
from docker_health_guardian.persistence import Database
from docker_health_guardian.reporting import generate_report


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "guardian.db"))
    database.initialize()
    return database


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


class TestGenerateReport:
    def test_empty_db_json_is_empty_list(self, db):
        assert json.loads(generate_report(db, "json")) == []

    def test_json_report_contains_incident_fields(self, db):
        incident = make_incident()
        db.upsert_incident(incident)
        rows = json.loads(generate_report(db, "json"))
        assert len(rows) == 1
        assert rows[0]["container_name"] == "app"
        assert rows[0]["severity"] == "high"

    def test_csv_report_has_header_and_row(self, db):
        db.upsert_incident(make_incident())
        csv_text = generate_report(db, "csv")
        lines = csv_text.strip().splitlines()
        assert lines[0].startswith("incident_id,")
        assert len(lines) == 2

    def test_empty_csv_report_is_empty_string(self, db):
        assert generate_report(db, "csv") == ""

    def test_html_report_contains_table(self, db):
        db.upsert_incident(make_incident())
        html = generate_report(db, "html")
        assert "<table>" in html
        assert "app" in html

    def test_html_report_escapes_container_name(self, db):
        db.upsert_incident(make_incident(container_name="<script>evil</script>"))
        html = generate_report(db, "html")
        assert "<script>evil</script>" not in html
        assert "&lt;script&gt;" in html

    def test_invalid_format_raises(self, db):
        with pytest.raises(ValueError):
            generate_report(db, "xml")

    def test_diagnosis_fields_populated_when_present(self, db):
        from docker_health_guardian.models import Confidence, Diagnosis

        incident = make_incident()
        incident.diagnosis = Diagnosis(
            likely_cause="test cause",
            confidence=Confidence.HIGH,
            recommended_action="do x",
            safe_to_automate=True,
        )
        db.upsert_incident(incident)
        rows = json.loads(generate_report(db, "json"))
        assert rows[0]["diagnosis_likely_cause"] == "test cause"
        assert rows[0]["diagnosis_confidence"] == "high"
