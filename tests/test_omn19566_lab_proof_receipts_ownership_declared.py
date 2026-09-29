# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declarations for lab proof receipts (OMN-19566)."""

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
    "src/omnimarket/nodes/node_projection_lab_proof_receipts/migrations/"
    "0000_create_lab_proof_receipts.sql"
)


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def test_lab_proof_receipts_is_declared_exactly_once() -> None:
    tables = list(_manifest()["db_io"]["db_tables"])
    matches = [row for row in tables if row["name"] == "lab_proof_receipts"]
    assert len(matches) == 1, (
        f"expected exactly one lab_proof_receipts declaration, got {matches}"
    )
    row = matches[0]
    assert row["schema"] == SCHEMA
    assert row["database_ref"] == "application"
    assert row["migration"] == CREATE_MIGRATION
    assert row["access"] == "read_write"


def test_lab_proof_receipts_cursor_sequence_is_declared_exactly_once() -> None:
    objects = list(_manifest()["database_objects"])
    sequence_name = "lab_proof_receipts_projection_cursor_seq"
    matches = [row for row in objects if row["name"] == sequence_name]
    assert len(matches) == 1, (
        f"expected exactly one {sequence_name} object, got {matches}"
    )
    row = matches[0]
    assert row["kind"] == "sequence"
    assert row["database_ref"] == "application"
    assert row["schema"] == SCHEMA
    assert row["domain"] == "OMNINODE_INTERNAL"
    assert row["owner_declaration"] == (
        "service:omnimarket_projection_migration_runner"
    )
    assert row["writers"] == []
