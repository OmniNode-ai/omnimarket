# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declarations for the goal-trace tables (OMN-20034, GC.11).

The entries are not documentation. The application database domain gate in
omnibase_infra resolves every relation a migration creates or grants on against
the manifest read here, at omnimarket's dev tip. Delete an entry and the
omnibase_infra pull request that vendors the goal-trace migration fails with
"requires exactly one ownership declaration", on another repository's pull
request, where nothing here would notice. That is what this test is for.

The cursor sequence is asserted on its own: the grant migration spells it by
literal name and the gate resolves a sequence as a target in its own right.
goal_traces has no cursor, so it has no sequence entry.
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
    "src/omnimarket/nodes/node_projection_goal_trace/migrations/"
    "0000_create_goal_trace_events.sql"
)
TABLES = {"goal_trace_events": "goal_trace_events", "goal_traces": "goal_traces"}
SEQUENCE = "goal_trace_events_projection_cursor_seq"


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


@pytest.mark.parametrize(("table", "role"), sorted(TABLES.items()))
def test_each_table_is_declared_exactly_once_with_its_create_migration(
    table: str, role: str
) -> None:
    rows = [r for r in _manifest()["db_io"]["db_tables"] if r["name"] == table]
    assert len(rows) == 1, f"expected exactly one {table} declaration, got {rows}"
    row = rows[0]
    assert row["schema"] == SCHEMA
    assert row["database_ref"] == "application"
    assert row["migration"] == CREATE_MIGRATION
    assert row["access"] == "read_write"
    assert row["role"] == role


def test_the_cursor_sequence_is_declared_as_a_sequence_object() -> None:
    rows = [r for r in _manifest()["database_objects"] if r["name"] == SEQUENCE]
    assert len(rows) == 1, f"expected exactly one {SEQUENCE} object, got {rows}"
    row = rows[0]
    assert row["kind"] == "sequence"
    assert row["schema"] == SCHEMA
    assert row["domain"] == "OMNINODE_INTERNAL"


def test_the_sequence_name_is_the_one_postgres_will_mint_for_the_events_table() -> None:
    assert SEQUENCE == "goal_trace_events_projection_cursor_seq"
    assert "goal_traces_projection_cursor_seq" not in {
        r["name"] for r in _manifest()["database_objects"]
    }
