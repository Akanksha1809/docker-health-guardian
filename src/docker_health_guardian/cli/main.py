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


if __name__ == "__main__":
    app()
