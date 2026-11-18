# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared overlay documents and writers of the ledger rows loader settings tests (OMN-24059)."""

from __future__ import annotations

import json
from pathlib import Path

COMMAND = [
    "{registry_root}/.onex-dispatch-venv/bin/python",
    "-m",
    "omnimarket.nodes.node_ledger_rows_loader_effect",
]
HEAD = "schema_version: ledger_rows_loader_hosts.v1\ntimeout_seconds: 45\nhosts:\n"


def host_row(host: str = "hp-gen8", command: object = COMMAND) -> str:
    return "  - " + json.dumps({"host": host, "command": command}) + "\n"


def write_overlays(
    root: Path, *, allowed: str = "true", hosts: str | None = None
) -> dict[str, str]:
    """Write both overlays under ``root`` and return the environment that names it."""
    (root / "node_ledger_rows_loader_effect").mkdir(parents=True)
    (root / "ledger_rows_loader_hosts").mkdir(parents=True)
    (root / "node_ledger_rows_loader_effect" / "overlay.yaml").write_text(
        f"file_fallback_allowed: {allowed}\njournal_env: ONEX_LEDGER_ROWS_LOADER_JOURNAL\n"
    )
    (root / "ledger_rows_loader_hosts" / "overlay.yaml").write_text(
        HEAD + host_row() if hosts is None else hosts
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}
