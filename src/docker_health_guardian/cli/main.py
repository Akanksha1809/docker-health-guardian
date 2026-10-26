"""Typer CLI entrypoint for Docker Health Guardian (`guardian` command)."""

from __future__ import annotations

from typing import Annotated

import typer

from docker_health_guardian.config import ConfigError, load_config
from docker_health_guardian.config.loader import DEFAULT_CONFIG_PATH

from .formatting import console, error_console, print_json, print_table

app = typer.Typer(
    name="guardian",
    help="Docker Health Guardian -- container health monitoring, diagnosis, and safe remediation.",
    no_args_is_help=True,
)

ConfigOption = Annotated[str, typer.Option("--config", "-c", help="Path to guardian.yaml")]
JsonOption = Annotated[bool, typer.Option("--json", help="Output JSON instead of a table")]


@app.command()
def containers(config: ConfigOption = DEFAULT_CONFIG_PATH, json_output: JsonOption = False) -> None:
    """List containers Docker Health Guardian can see, with current state and resource usage."""
    from docker_health_guardian.collector import DockerCollector, DockerUnavailableError

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        error_console.print(str(exc))
        raise typer.Exit(code=2) from exc

    try:
        collector = DockerCollector(cfg.docker)
        snapshots = collector.list_container_snapshots()
    except DockerUnavailableError as exc:
        error_console.print(f"Docker daemon unreachable: {exc}")
        raise typer.Exit(code=3) from exc

    if json_output:
        print_json([s.model_dump(mode="json") for s in snapshots])
        return

    rows = []
    for s in snapshots:
        cpu = f"{s.stats.cpu_percent}%" if s.stats.cpu_percent is not None else "n/a"
        mem = f"{s.stats.memory_percent}%" if s.stats.memory_percent is not None else "n/a"
        rows.append(
            [
                s.name,
                s.state.value,
                s.health_status.value,
                s.restart_count,
                cpu,
                mem,
                "excluded" if s.monitoring_excluded else "monitored",
            ]
        )
    print_table(
        "Containers",
        ["Name", "State", "Health", "Restarts", "CPU", "Memory", "Monitoring"],
        rows,
    )


@app.command("config-validate", help="Validate the configuration file and exit.")
def config_validate(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    try:
        cfg = load_config(config)
    except ConfigError as exc:
        error_console.print(str(exc))
        raise typer.Exit(code=2) from exc
    console.print(f"[green]Configuration at {config} is valid.[/green]")
    console.print(
        f"mode={cfg.guardian.mode} remediation.enabled={cfg.remediation.enabled} "
        f"remediation.dry_run={cfg.remediation.dry_run}"
    )


@app.command()
def start(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Run the monitoring loop (and, if enabled, the read-only API) in the foreground."""
    import logging
    import threading

    from docker_health_guardian.scheduler import build_scheduler

    try:
        cfg = load_config(config)
    except ConfigError as exc:
        error_console.print(str(exc))
        raise typer.Exit(code=2) from exc

    logging.basicConfig(level=cfg.guardian.log_level)
    scheduler = build_scheduler(cfg)

    api_thread = None
    api_server = None
    if cfg.api.enabled:
        import uvicorn

        from docker_health_guardian.api.app import app as fastapi_app
        from docker_health_guardian.api.app import configure as configure_api

        configure_api(config)
        uvicorn_config = uvicorn.Config(
            fastapi_app, host=cfg.api.host, port=cfg.api.port, log_level="warning"
        )
        api_server = uvicorn.Server(uvicorn_config)
        api_thread = threading.Thread(target=api_server.run, daemon=True)
        api_thread.start()
        console.print(f"[green]API listening on http://{cfg.api.host}:{cfg.api.port}[/green]")

    try:
        scheduler.run_forever()
    finally:
        if api_server is not None:
            api_server.should_exit = True
        if api_thread is not None:
            api_thread.join(timeout=5)


@app.command("status")
def status(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """One-shot snapshot: open incidents, remediation mode, container counts."""
    from docker_health_guardian.collector import DockerCollector, DockerUnavailableError

    db, cfg = _open_db(config)
    console.print(
        f"mode={cfg.guardian.mode} remediation.enabled={cfg.remediation.enabled} "
        f"dry_run={cfg.remediation.dry_run}"
    )
    console.print(f"open incidents: {db.count_open_incidents()}")
    try:
        collector = DockerCollector(cfg.docker)
        snapshots = collector.list_container_snapshots()
        console.print(f"containers observed: {len(snapshots)}")
    except DockerUnavailableError as exc:
        error_console.print(f"Docker daemon unreachable: {exc}")


@app.command("self-check", help="Run startup diagnostics: config, Docker daemon, persistence.")
def self_check(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    from docker_health_guardian.collector import DockerCollector

    ok = True

    try:
        cfg = load_config(config)
        console.print("[green]OK[/green]  configuration valid")
    except ConfigError as exc:
        error_console.print(f"FAIL configuration invalid: {exc}")
        raise typer.Exit(code=2) from exc

    try:
        collector = DockerCollector(cfg.docker)
        if collector.ping():
            console.print("[green]OK[/green]  docker daemon reachable")
        else:
            console.print("[red]FAIL[/red] docker daemon not reachable")
            ok = False
    except Exception as exc:  # noqa: BLE001 -- self-check must report, not crash
        console.print(f"[red]FAIL[/red] docker daemon error: {exc}")
        ok = False

    try:
        from docker_health_guardian.persistence import Database

        db = Database(cfg.persistence.db_path)
        db.initialize()
        console.print(f"[green]OK[/green]  persistence initialized at {cfg.persistence.db_path}")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]FAIL[/red] persistence error: {exc}")
        ok = False

    if not ok:
        raise typer.Exit(code=1)
    console.print("[bold green]All checks passed.[/bold green]")


rules_app = typer.Typer(help="Inspect the detection rule catalog.")
app.add_typer(rules_app, name="rules")


@rules_app.command("list")
def rules_list(json_output: JsonOption = False) -> None:
    """List all detection rules with severity, category, and remediation permission."""
    from docker_health_guardian.detection import RULES

    if json_output:
        print_json(
            [
                {
                    "rule_id": r.rule_id,
                    "severity": r.severity.value,
                    "category": r.category,
                    "remediation_permitted": r.remediation_permitted,
                    "cooldown_seconds": r.cooldown_seconds,
                    "description": r.description,
                }
                for r in RULES.values()
            ]
        )
        return
    rows = [
        [r.rule_id, r.category, r.severity.value, r.remediation_permitted, r.cooldown_seconds]
        for r in RULES.values()
    ]
    print_table(
        "Detection Rules",
        ["Rule ID", "Category", "Severity", "Remediation OK", "Cooldown (s)"],
        rows,
    )


@rules_app.command("validate")
def rules_validate(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Validate that detection thresholds in the config are internally consistent."""
    try:
        load_config(config)
    except ConfigError as exc:
        error_console.print(str(exc))
        raise typer.Exit(code=2) from exc
    console.print("[green]All detection rules and thresholds are valid.[/green]")


incidents_app = typer.Typer(help="Inspect incidents.")
app.add_typer(incidents_app, name="incidents")


def _open_db(config_path: str):
    from docker_health_guardian.persistence import Database

    cfg = load_config(config_path)
    db = Database(cfg.persistence.db_path)
    db.initialize()
    return db, cfg


@incidents_app.command("list")
def incidents_list(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    state: Annotated[str | None, typer.Option(help="Filter by lifecycle state")] = None,
    severity: Annotated[str | None, typer.Option(help="Filter by severity")] = None,
    limit: Annotated[int, typer.Option(help="Max rows to return")] = 50,
    json_output: JsonOption = False,
) -> None:
    db, _ = _open_db(config)
    incidents = db.list_incidents(state=state, severity=severity, limit=limit)
    if json_output:
        print_json([i.model_dump(mode="json") for i in incidents])
        return
    rows = [
        [
            i.incident_id[:8],
            i.container_name,
            i.rule_id,
            i.severity.value,
            i.state.value,
            i.last_seen,
        ]
        for i in incidents
    ]
    print_table("Incidents", ["ID", "Container", "Rule", "Severity", "State", "Last Seen"], rows)


@app.command("incident")
def incident_show(
    incident_id: str, config: ConfigOption = DEFAULT_CONFIG_PATH, json_output: JsonOption = False
) -> None:
    """Show full detail for one incident, including its event timeline."""
    db, _ = _open_db(config)
    incident = db.get_incident(incident_id)
    if incident is None:
        error_console.print(f"No incident found with id {incident_id}")
        raise typer.Exit(code=1)
    events = db.list_events(incident.incident_id)
    attempts = db.list_remediation_attempts(incident_id=incident.incident_id)
    if json_output:
        print_json(
            {
                "incident": incident.model_dump(mode="json"),
                "events": [e.model_dump(mode="json") for e in events],
                "remediation_attempts": [a.model_dump(mode="json") for a in attempts],
            }
        )
        return
    console.print(f"[bold]{incident.incident_id}[/bold] -- {incident.container_name}")
    console.print(
        f"rule={incident.rule_id} severity={incident.severity.value} "
        f"state={incident.state.value}"
    )
    console.print(f"first_seen={incident.first_seen} last_seen={incident.last_seen}")
    if incident.diagnosis:
        console.print(f"[bold]diagnosis:[/bold] {incident.diagnosis.likely_cause} "
                      f"(confidence={incident.diagnosis.confidence.value})")
        console.print(f"recommended: {incident.diagnosis.recommended_action}")
    print_table(
        "Events", ["Time", "Kind", "Detail"], [[e.timestamp, e.kind, e.detail] for e in events]
    )
    if attempts:
        print_table(
            "Remediation Attempts",
            ["Started", "Action", "Dry Run", "Outcome", "Recovery Verified"],
            [
                [a.started_at, a.action, a.dry_run, a.outcome.value, a.recovery_verified]
                for a in attempts
            ],
        )


remediation_app = typer.Typer(help="Inspect and control remediation.")
app.add_typer(remediation_app, name="remediation")


@remediation_app.command("history")
def remediation_history(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    container: Annotated[str | None, typer.Option(help="Filter by container name")] = None,
    limit: Annotated[int, typer.Option(help="Max rows to return")] = 50,
    json_output: JsonOption = False,
) -> None:
    db, _ = _open_db(config)
    attempts = db.list_remediation_attempts(container_name=container, limit=limit)
    if json_output:
        print_json([a.model_dump(mode="json") for a in attempts])
        return
    rows = [
        [a.started_at, a.container_name, a.action, a.dry_run, a.outcome.value, a.recovery_verified]
        for a in attempts
    ]
    print_table(
        "Remediation History",
        ["Started", "Container", "Action", "Dry Run", "Outcome", "Recovery Verified"],
        rows,
    )


@remediation_app.command("dry-run")
def remediation_dry_run(container: str, config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Show whether `container` is currently eligible for automated restart, without acting."""
    from docker_health_guardian.remediation import RemediationEngine

    db, cfg = _open_db(config)
    engine = RemediationEngine(db)
    from docker_health_guardian.models import Incident, Severity

    probe = Incident(
        container_name=container,
        container_id=container,
        rule_id="health_unhealthy",
        incident_type="health_unhealthy",
        severity=Severity.HIGH,
    )
    eligible, reason = engine.check_eligibility(probe, cfg.remediation)
    status = "[green]ELIGIBLE[/green]" if eligible else "[red]NOT ELIGIBLE[/red]"
    console.print(f"{container}: {status}")
    console.print(f"reason: {reason}")
    console.print(f"remediation.dry_run={cfg.remediation.dry_run} (even if eligible, no restart "
                  "will execute while dry_run is true)")


@remediation_app.command("enable")
def remediation_enable(container: str, config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Explicitly allow restart remediation for `container`, overriding the static allowlist."""
    db, _ = _open_db(config)
    db.set_container_override(container, enabled=True)
    console.print(f"[green]Remediation enabled for '{container}'.[/green] "
                  "(protected_containers, if set, still takes precedence)")


@remediation_app.command("disable")
def remediation_disable(container: str, config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Explicitly forbid restart remediation for `container`, regardless of the allowlist."""
    db, _ = _open_db(config)
    db.set_container_override(container, enabled=False)
    console.print(f"[yellow]Remediation disabled for '{container}'.[/yellow]")


@app.command()
def report(
    config: ConfigOption = DEFAULT_CONFIG_PATH,
    output_format: Annotated[str, typer.Option("--format", help="json|csv|html")] = "json",
    output_path: Annotated[str | None, typer.Option("--output", "-o", help="File to write")] = None,
) -> None:
    """Generate an incident/remediation report (JSON, CSV, or an HTML summary)."""
    from docker_health_guardian.reporting import generate_report

    db, _ = _open_db(config)
    content = generate_report(db, output_format)
    if output_path:
        from pathlib import Path

        Path(output_path).write_text(content)
        console.print(f"[green]Report written to {output_path}[/green]")
    else:
        console.print(content)


@app.command("cleanup")
def cleanup(config: ConfigOption = DEFAULT_CONFIG_PATH) -> None:
    """Delete RESOLVED/SUPPRESSED incidents older than persistence.retention_days.

    Open incidents are never touched regardless of age. Intended to be run
    periodically (e.g. via cron or a scheduled CI job) -- it is not run
    automatically by `guardian start`.
    """
    db, cfg = _open_db(config)
    removed = db.cleanup_retention(cfg.persistence.retention_days)
    console.print(
        f"[green]Removed {removed} incident(s) older than "
        f"{cfg.persistence.retention_days} days.[/green]"
    )


if __name__ == "__main__":
    app()
