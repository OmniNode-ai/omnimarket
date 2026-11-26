# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Acceptance criterion AC5 of OMN-26623.

scripts/application-relation-ownership.yaml declares the weekly delegation-quality
view exactly once, so omnibase_infra's application SQL gate finds one owner for
it and can vendor migration 0070. Failure modes: the entry is missing (as on
dev), declared twice, declared under another kind, schema, domain or owner, or
in both the relation_evidence and database_objects sections.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

MANIFEST = (
    Path(__file__).resolve().parents[2] / "scripts/application-relation-ownership.yaml"
)
RUNNER = "service:omnimarket_projection_migration_runner"
MIGRATION = "src/omnimarket/nodes/node_projection_delegation/migrations/0070_delegation_quality_weekly_view.sql"
NAME = "projection_delegation_quality_weekly"


def _section(key: str) -> list[dict[str, Any]]:
    with MANIFEST.open() as stream:
        data = yaml.safe_load(stream)
    return list(data[key])


def _entries() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for section_name in ("database_objects", "relation_evidence"):
        for row in _section(section_name):
            if row.get("name") == NAME:
                entries.append(row)
    return entries


def test_the_weekly_view_is_declared_exactly_once() -> None:
    entries = _entries()
    assert len(entries) == 1, entries


def test_the_weekly_view_is_a_tenant_view_owned_by_the_migration_runner() -> None:
    rows = [row for row in _section("relation_evidence") if row.get("name") == NAME]
    assert len(rows) == 1, rows
    (row,) = rows
    assert row["kind"] == "view", row
    assert row["database_ref"] == "application", row
    assert row["schema"] == "public", row
    assert row["domain"] == "TENANT", row
    assert row["owner_declaration"] == RUNNER, row
    assert row["writers"] == [], row
    assert row["producer"] == MIGRATION, row
    assert row["authoritative_sources"] == [MIGRATION], row
    assert row["readers"] == [
        "src/omnimarket/nodes/node_projection_delegation/handlers/handler_delegation.py",
        "src/omnimarket/nodes/node_projection_delegation/handlers/handler_projection_delegation.py",
    ], row


def test_the_weekly_view_names_its_ticket_and_the_infra_vendoring() -> None:
    rows = [row for row in _section("relation_evidence") if row.get("name") == NAME]
    (row,) = rows
    assert "OMN-26623" in row["classification_evidence"], row
    assert "omnibase_infra" in row["classification_evidence"], row
