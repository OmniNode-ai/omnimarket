# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declarations for the branch deploy state (OMN-24118).

The application database domain gate in omnibase_infra resolves every relation a
migration creates against the manifest read here. Migration 116 creates
public.branch_deploy_state and its trigger helper; delete either entry and the
omnibase_infra pull request that carries migration 116 fails with "lacks an
authoritative ownership declaration", on another repository's pull request,
where nothing here would notice. That is what this test is for.
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
CREATE_MIGRATION = "docker/migrations/forward/116_create_branch_deploy_state.sql"
TABLE = "branch_deploy_state"
FUNCTION = "refresh_branch_deploy_state_updated_at"
AUTHORITY = "service:omnimarket_projection_migration_runner"


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def test_the_table_is_declared_exactly_once_with_its_create_migration() -> None:
    rows = [r for r in _manifest()["db_io"]["db_tables"] if r["name"] == TABLE]
    assert len(rows) == 1, f"expected exactly one {TABLE} declaration, got {rows}"
    row = rows[0]
    assert row["schema"] == "public"
    assert row["database_ref"] == "application"
    assert row["migration"] == CREATE_MIGRATION
    assert row["access"] == "read_write"
    assert row["role"] == "branch_deploy_orchestrator_state"


def test_the_trigger_helper_is_declared_exactly_once_as_a_function() -> None:
    rows = [r for r in _manifest()["database_objects"] if r["name"] == FUNCTION]
    assert len(rows) == 1, f"expected exactly one {FUNCTION} object, got {rows}"
    row = rows[0]
    assert row["kind"] == "function"
    assert row["schema"] == "public"
    assert row["function_signature"] == "()"
    assert row["owner_declaration"] == AUTHORITY
