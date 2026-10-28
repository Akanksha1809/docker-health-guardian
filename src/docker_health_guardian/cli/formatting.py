"""Shared Rich table/JSON helpers for CLI commands."""

from __future__ import annotations

import json
from typing import Any

from rich.console import Console
from rich.table import Table

console = Console()
error_console = Console(stderr=True, style="bold red")


def print_table(title: str, columns: list[str], rows: list[list[Any]]) -> None:
    table = Table(title=title)
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*(str(cell) for cell in row))
    console.print(table)


def print_json(data: Any) -> None:
    # Deliberately bypasses the Rich console: it word-wraps long lines to
    # the terminal width, which can inject literal newlines into the
    # middle of a JSON string value and corrupt otherwise-valid JSON.
    # Machine-readable output must never be reflowed.
    print(json.dumps(data, indent=2, default=str))
