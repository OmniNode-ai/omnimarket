# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What omnibase_infra needs from omnimarket to vendor two projections (OMN-20578).

omnibase_infra vendors the migrations of node_projection_routing_feedback and
node_projection_alert_channel_liveness. Its shape-reconciliation gate refuses a
guarded CREATE TABLE whose declared columns are never reconciled, and its
application SQL gate refuses a created relation that this repository's
ownership manifest does not declare exactly once.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST = REPO_ROOT / "scripts/application-relation-ownership.yaml"
NODES = REPO_ROOT / "src/omnimarket/nodes"

ROUTING_FEEDBACK_CREATE = (
    "src/omnimarket/nodes/node_projection_routing_feedback/migrations/"
    "0000_create_delegation_routing_feedback.sql"
)
ALERT_LIVENESS_CREATE = (
    "src/omnimarket/nodes/node_projection_alert_channel_liveness/migrations/"
    "0000_create_alert_channel_liveness_verdicts.sql"
)


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def _declared_columns(sql: str, table: str) -> list[str]:
    body = re.search(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table)} \((.*?)\n\);",
        sql,
        re.DOTALL,
    )
    assert body is not None, f"no guarded CREATE TABLE for {table}"
    columns = []
    for line in body.group(1).splitlines():
        token = line.strip().split(" ", 1)[0]
        if token and token.upper() not in {"PRIMARY", "CONSTRAINT", "--"}:
            columns.append(token)
    return columns


def test_routing_feedback_reconciles_every_declared_column() -> None:
    sql = (REPO_ROOT / ROUTING_FEEDBACK_CREATE).read_text()
    table = "public.delegation_routing_feedback"
    columns = _declared_columns(sql, table)
    assert len(columns) == 11
    missing = [
        column
        for column in columns
        if f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} " not in sql
    ]
    assert missing == []


@pytest.mark.parametrize(
    ("name", "schema", "migration", "access"),
    [
        (
            "delegation_routing_feedback",
            "public",
            ROUTING_FEEDBACK_CREATE,
            "read_write",
        ),
        (
            "alert_channel_liveness_verdicts",
            "omninode_internal",
            ALERT_LIVENESS_CREATE,
            "write",
        ),
    ],
)
def test_table_is_declared_exactly_once(
    name: str, schema: str, migration: str, access: str
) -> None:
    matches = [row for row in _manifest()["db_io"]["db_tables"] if row["name"] == name]
    assert len(matches) == 1, matches
    row = matches[0]
    assert row["database_ref"] == "application"
    assert row["schema"] == schema
    assert row["migration"] == migration
    assert row["access"] == access
    assert (REPO_ROOT / migration).is_file()


def test_alert_liveness_cursor_sequence_is_declared_exactly_once() -> None:
    name = "alert_channel_liveness_verdicts_projection_cursor_seq"
    matches = [row for row in _manifest()["database_objects"] if row["name"] == name]
    assert len(matches) == 1, matches
    row = matches[0]
    assert row["kind"] == "sequence"
    assert row["database_ref"] == "application"
    assert row["schema"] == "omninode_internal"
    assert row["domain"] == "OMNINODE_INTERNAL"
    assert row["owner_declaration"] == "service:omnimarket_projection_migration_runner"
    assert row["writers"] == []
