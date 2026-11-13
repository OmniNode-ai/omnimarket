# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Synthetic lab host tables for the host table compute tests (OMN-22251): no real host names."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from omnimarket.models.overlay_keys import ModelLabHostsTable

EXCLUDED = "excluded-row"
REASON = "ruled out for the pilot"


def row(name: str, **cells: Any) -> dict[str, Any]:
    return {"name": name, "target": f"{name}.lan", **cells}


def document(*rows: dict[str, Any]) -> dict[str, Any]:
    return {"hosts": list(rows)}


def table(*rows: dict[str, Any]) -> ModelLabHostsTable:
    return ModelLabHostsTable.model_validate(document(*rows))


def three_hosts() -> tuple[dict[str, Any], ...]:
    return (
        row("admitted-a", cap=4),
        row(EXCLUDED, cap=3, available=False, unavailable_reason=REASON),
        row("admitted-b"),
    )


def write_table(path: Path, *rows: dict[str, Any]) -> dict[str, str]:
    """Write the table file and return the environment that pins it, as the overlay store's projection does."""
    path.write_text(yaml.safe_dump(document(*rows)), encoding="utf-8")
    return {"ONEX_LAB_RUN_HOSTS": str(path)}
