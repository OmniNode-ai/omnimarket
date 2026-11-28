# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the lab host table a key registration binds a host from."""

from __future__ import annotations

from pathlib import Path

import click
import yaml


def lab_host_rows(table: Path) -> list[tuple[str, str]]:
    """Each row's ``(name, target)``, or a refusal naming the table."""
    try:
        document = yaml.safe_load(table.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise click.ClickException(
            f"the lab host table {table} could not be read ({exc}); nothing was stored."
        ) from exc
    rows = document.get("hosts") if isinstance(document, dict) else None
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and row.get("name") for row in rows
    ):
        raise click.ClickException(
            f"the lab host table {table} has no hosts with a name; nothing was stored."
        )
    return [(str(row["name"]), str(row.get("target") or "")) for row in rows]


__all__: list[str] = ["lab_host_rows"]
