# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declaration for worktree reconcile hosts (OMN-19399)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

MANIFEST = (
    Path(__file__).resolve().parents[1] / "scripts/application-relation-ownership.yaml"
)

CREATE_MIGRATION = (
    "src/omnimarket/nodes/node_projection_worktree_reconcile/migrations/"
    "0000_create_worktree_reconcile_hosts.sql"
)


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def test_worktree_reconcile_hosts_is_declared_exactly_once() -> None:
    tables = list(_manifest()["db_io"]["db_tables"])
    matches = [row for row in tables if row["name"] == "worktree_reconcile_hosts"]
    assert len(matches) == 1, (
        f"expected exactly one worktree_reconcile_hosts declaration, got {matches}"
    )
    row = matches[0]
    assert row["schema"] == "omninode_internal"
    assert row["database_ref"] == "application"
    assert row["migration"] == CREATE_MIGRATION
    assert row["access"] == "read_write"
    assert row["role"] == "worktree_reconcile_hosts"
