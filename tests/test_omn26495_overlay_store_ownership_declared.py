# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declaration for the overlay store table (OMN-26495).

The application database domain gate in omnibase_infra resolves every relation a
changed migration touches against the manifest read here. The overlay store's
migration 0002 alters omninode_internal.overlay_store; delete the entry and the
omnibase_infra pull request that vendors that migration fails with "application
target omninode_internal.overlay_store requires exactly one ownership declaration",
on another repository's pull request, where nothing here would notice. That is
what this test is for.
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
TABLE = "overlay_store"


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def test_the_table_is_declared_exactly_once_with_its_create_migration() -> None:
    rows = [r for r in _manifest()["db_io"]["db_tables"] if r["name"] == TABLE]
    assert len(rows) == 1, f"expected exactly one {TABLE} declaration, got {rows}"
    row = rows[0]
    assert row["schema"] == "omninode_internal"
    assert row["database_ref"] == "application"
    assert row["access"] == "read_write"
    assert row["role"] == "overlay_store"
    assert (ROOT / row["migration"]).is_file(), row["migration"]
    assert row["migration"].startswith(
        "src/omnimarket/nodes/node_projection_overlay_store/migrations/0000_"
    )


def test_the_declared_table_is_the_one_the_node_contract_names() -> None:
    with open(NODE / "contract.yaml") as handle:
        contract = yaml.safe_load(handle)
    contract_tables = {t["name"]: t for t in contract["db_io"]["db_tables"]}
    assert TABLE in contract_tables
    row = next(r for r in _manifest()["db_io"]["db_tables"] if r["name"] == TABLE)
    assert row["schema"] == contract_tables[TABLE]["schema"]
    assert row["database_ref"] == contract_tables[TABLE]["database_ref"]


def test_every_migration_of_the_node_alters_a_declared_table() -> None:
    declared = {r["name"] for r in _manifest()["db_io"]["db_tables"]}
    migrations = sorted((NODE / "migrations").glob("*.sql"))
    assert migrations, "the node carries no migrations"
    for migration in migrations:
        text = migration.read_text(encoding="utf-8")
        assert f"omninode_internal.{TABLE}" in text, migration.name
    assert TABLE in declared
