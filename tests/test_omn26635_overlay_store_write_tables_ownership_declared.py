# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ownership declaration for the overlay store's four write tables (OMN-26635).

The runtime refuses to boot a projection binding whose principal lacks the write
privileges declared for a table the node contract names. The privileges come from
the application relation manifest read here, so a table the node contract writes
but the manifest omits fails every composed-runtime boot with "lacks declared
write privileges: INSERT, SELECT, UPDATE on table omninode_internal.<table>".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "scripts/application-relation-ownership.yaml"
NODE = ROOT / "src/omnimarket/nodes/node_projection_overlay_store"
TABLES = (
    "overlay_store_revision",
    "overlay_store_write",
    "overlay_store_provenance",
    "overlay_store_audit",
)


def _manifest_rows() -> list[dict[str, Any]]:
    with open(MANIFEST) as handle:
        return list(yaml.safe_load(handle)["db_io"]["db_tables"])


@pytest.mark.parametrize("table", TABLES)
def test_write_table_is_declared_once_with_its_create_migration(table: str) -> None:
    rows = [r for r in _manifest_rows() if r["name"] == table]
    assert len(rows) == 1, f"expected exactly one {table} declaration, got {rows}"
    row = rows[0]
    assert row["schema"] == "omninode_internal"
    assert row["database_ref"] == "application"
    assert row["access"] == "write"
    assert row["role"] == table
    assert (ROOT / row["migration"]).is_file(), row["migration"]
    assert row["migration"].endswith("0003_overlay_store_revision_provenance_audit.sql")


@pytest.mark.parametrize("table", TABLES)
def test_manifest_row_mirrors_the_node_contract(table: str) -> None:
    with open(NODE / "contract.yaml") as handle:
        contract = yaml.safe_load(handle)
    declared = {t["name"]: t for t in contract["db_io"]["db_tables"]}
    row = next(r for r in _manifest_rows() if r["name"] == table)
    assert row["schema"] == declared[table]["schema"]
    assert row["access"] == declared[table]["access"]
    assert row["role"] == declared[table]["role"]
