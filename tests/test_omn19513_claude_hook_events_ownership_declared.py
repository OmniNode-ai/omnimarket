# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declarations for the all-hooks capture tables (OMN-19513).

WHY A TEST FOR YAML ENTRIES
    `check_application_database_sql.py` (the OMN-15361 SQL ownership gate)
    resolves every application object a migration creates or grants on against
    this manifest, from inside omnibase_infra. omnibase_infra#4169 vendors
    node_projection_claude_hook_events' two migrations, which create two tables
    and grant USAGE on two BIGSERIAL cursor sequences by literal name. The gate
    refused all four objects on that pull request's head 27580e049 ("requires
    exactly one ownership declaration"), and nothing in this repository noticed.

WHY THE SEQUENCES ARE ASSERTED SEPARATELY
    The first version of this declaration named only the two tables. The gate
    treats a sequence named by a GRANT as a target in its own right and does
    not infer it from its owning table (the OMN-18999 correction, recorded
    beside prod_promotion_gate_decisions_projection_cursor_seq). A table-only
    declaration leaves two of the four findings standing, so each sequence has
    its own assertion here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

MANIFEST = (
    Path(__file__).resolve().parents[1] / "scripts/application-relation-ownership.yaml"
)

SCHEMA = "omninode_internal"
CREATE_MIGRATION = (
    "src/omnimarket/nodes/node_projection_claude_hook_events/migrations/"
    "0000_create_claude_hook_events.sql"
)
TABLES = ("claude_hook_events", "claude_agent_spans")


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def _tables() -> list[dict[str, Any]]:
    return list(_manifest()["db_io"]["db_tables"])


def _objects() -> list[dict[str, Any]]:
    return list(_manifest()["database_objects"])


@pytest.mark.parametrize("table", TABLES)
def test_each_table_is_declared_exactly_once(table: str) -> None:
    matches = [row for row in _tables() if row["name"] == table]
    assert len(matches) == 1, f"expected exactly one {table} declaration, got {matches}"
    row = matches[0]
    assert row["schema"] == SCHEMA
    assert row["database_ref"] == "application"
    assert row["migration"] == CREATE_MIGRATION
    assert row["access"] == "read_write"


@pytest.mark.parametrize("table", TABLES)
def test_each_cursor_sequence_is_declared_exactly_once_as_a_sequence(
    table: str,
) -> None:
    """The half a table-only reading misses, and the half #4169 failed on."""
    sequence = f"{table}_projection_cursor_seq"
    matches = [row for row in _objects() if row["name"] == sequence]
    assert len(matches) == 1, f"expected exactly one {sequence} object, got {matches}"
    row = matches[0]
    assert row["kind"] == "sequence"
    assert row["database_ref"] == "application"
    assert row["schema"] == SCHEMA
    assert row["domain"] == "OMNINODE_INTERNAL"
    assert row["owner_declaration"] == "service:omnimarket_projection_migration_runner"
