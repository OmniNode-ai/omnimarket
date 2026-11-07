# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host row's ``ledger_path`` column: the launcher reads it, so the table model must carry it.

Without the column every placement that validates the table refused the whole table
("the lab host table could not be read ... invalid: fields hosts.2.ledger_path").
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.handlers.overlay_keys import resolve_lab_hosts_table

pytestmark = pytest.mark.unit


def _write(tmp_path: Path, ledger_path: str) -> dict[str, str]:
    table = tmp_path / "hosts.yaml"
    table.write_text(
        yaml.safe_dump(
            {"hosts": [{"name": "h1", "target": "h1", "ledger_path": ledger_path}]}
        ),
        encoding="utf-8",
    )
    return {"ONEX_LAB_RUN_HOSTS": str(table)}


def test_a_row_that_declares_a_ledger_path_loads_with_it(tmp_path: Path) -> None:
    table = resolve_lab_hosts_table(_write(tmp_path, "$OMNI_HOME/docs/ledger.md"))
    assert table.hosts[0].ledger_path == "$OMNI_HOME/docs/ledger.md"


def test_a_relative_ledger_path_refuses_as_the_launcher_refuses_it(
    tmp_path: Path,
) -> None:
    with pytest.raises(NodeOverlayError, match=r"hosts\.0\.ledger_path"):
        resolve_lab_hosts_table(_write(tmp_path, "docs/ledger.md"))
