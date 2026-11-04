# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The per-day delegation view and the delegation_events walk cursor, owned in this manifest (OMN-21970).

omnibase_infra vendors node_projection_delegation's 0065 and 0066 migrations before this repo ships them,
and its application SQL gate needs every object those migrations create or grant to have exactly one
correctly typed owner here: the cursor sequence 0065 grants (a sequence, not covered by the
delegation_events table entry) and the view 0066 creates (a view, not a table). Failure modes: either
object is missing, declared twice, declared as the wrong kind, or declared under another schema, domain
or owner than the tenant projection's migration runner.
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
RUNNER = "service:omnimarket_projection_migration_runner"
DAILY_VIEW_MIGRATION = "src/omnimarket/nodes/node_projection_delegation/migrations/0066_delegation_daily_view.sql"


def _section(key: str) -> list[dict[str, Any]]:
    with open(MANIFEST) as handle:
        return list(dict(yaml.safe_load(handle))[key])


def _only(name: str, key: str) -> dict[str, Any]:
    # Sequences live under database_objects and views under relation_evidence, beside the existing
    # delegation views; a name declared in the other section as well would be a second owner.
    everywhere = [
        row
        for section in ("database_objects", "relation_evidence")
        for row in _section(section)
        if row["name"] == name
    ]
    assert len(everywhere) == 1, (
        f"expected exactly one {name} declaration, got {everywhere}"
    )
    matches = [row for row in _section(key) if row["name"] == name]
    assert len(matches) == 1, f"expected {name} under {key}, got {matches}"
    return matches[0]


def test_delegation_events_cursor_sequence_is_declared_once_as_a_tenant_sequence() -> (
    None
):
    row = _only("delegation_events_projection_cursor_seq", "database_objects")
    assert row["kind"] == "sequence"
    assert row["database_ref"] == "application"
    assert row["schema"] == "public"
    assert row["domain"] == "TENANT"
    assert row["owner_declaration"] == RUNNER
    assert row["writers"] == []


def test_daily_view_is_declared_once_as_a_tenant_view_produced_by_0066() -> None:
    row = _only("projection_delegation_daily", "relation_evidence")
    assert row["kind"] == "view"
    assert row["database_ref"] == "application"
    assert row["schema"] == "public"
    assert row["domain"] == "TENANT"
    assert row["owner_declaration"] == RUNNER
    assert row["producer"] == DAILY_VIEW_MIGRATION
    assert row["authoritative_sources"] == [DAILY_VIEW_MIGRATION]
    assert row["writers"] == []
    assert row["readers"], "the view is read by the delegation projection handlers"
