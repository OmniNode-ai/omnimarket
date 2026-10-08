# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pin the reconciled schema for OMN-20287 step 1.

Cross-run lineage on run rows uses the OMN-20606 columns.
"""

from __future__ import annotations

import re
from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.enums.enum_delegation_attempt_kind import EnumDelegationAttemptKind
from omnimarket.enums.enum_delegation_lineage_kind import EnumDelegationLineageKind
from omnimarket.models.delegation.delegation_lineage import LINEAGE_KEYS
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_lineage_fold import (
    HandlerDelegationLineageFold,
)
from omnimarket.projection.sqlite_database import _DELEGATION_EVENTS_DECLARED_COLUMNS

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_projection_delegation"
)
_MIGRATIONS = _NODE / "migrations"
_LINEAGE_COLUMNS = frozenset(
    {"parent_correlation_id", "lineage_kind", "parent_failure_cause"}
)
_ADDS_ATTEMPT_KIND = re.compile(
    r"ADD\s+COLUMN\s+(IF\s+NOT\s+EXISTS\s+)?attempt_kind\b", re.IGNORECASE
)


def test_only_canonical_migration_adds_parent_correlation_id() -> None:
    migrations = [
        path
        for path in _MIGRATIONS.rglob("*")
        if path.is_file()
        and "ADD COLUMN IF NOT EXISTS parent_correlation_id"
        in path.read_text(encoding="utf-8")
    ]
    assert migrations == [_MIGRATIONS / "0055_delegation_events_lineage.sql"]


def test_migrations_drop_attempt_kind_and_competing_migration() -> None:
    assert not (_MIGRATIONS / "0055_delegation_events_run_lineage.sql").exists()
    for path in _MIGRATIONS.rglob("*"):
        if path.is_file():
            sql = path.read_text(encoding="utf-8")
            assert not _ADDS_ATTEMPT_KIND.search(sql), path


def test_canonical_migration_adds_nullable_text_lineage_columns() -> None:
    sql = (_MIGRATIONS / "0055_delegation_events_lineage.sql").read_text(
        encoding="utf-8"
    )
    for column in _LINEAGE_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column} TEXT" in sql
    # The partial index uses IS NOT NULL; column declarations must be nullable.
    alter_table = sql.split("ALTER TABLE delegation_events", 1)[1].split(";", 1)[0]
    assert "NOT NULL" not in alter_table


def test_node_contract_declares_only_canonical_lineage_columns() -> None:
    lines = {
        line.strip()
        for line in (_NODE / "contract.yaml").read_text(encoding="utf-8").splitlines()
    }
    for column in _LINEAGE_COLUMNS:
        assert f"- {column}" in lines
    assert "- attempt_kind" not in lines


def test_sqlite_declares_only_canonical_lineage_columns() -> None:
    assert set(_DELEGATION_EVENTS_DECLARED_COLUMNS) >= _LINEAGE_COLUMNS
    assert "attempt_kind" not in _DELEGATION_EVENTS_DECLARED_COLUMNS


def test_terminal_model_declares_only_canonical_lineage_columns() -> None:
    fields = ModelDelegateSkillTerminalProjection.model_fields
    assert fields.keys() >= _LINEAGE_COLUMNS
    assert "attempt_kind" not in fields


def test_lineage_keys_are_exactly_the_canonical_columns() -> None:
    assert LINEAGE_KEYS == _LINEAGE_COLUMNS


def test_cross_run_and_in_ladder_vocabularies_are_disjoint() -> None:
    lineage_values = {kind.value for kind in EnumDelegationLineageKind}
    attempt_values = {kind.value for kind in EnumDelegationAttemptKind}
    assert lineage_values == {"fallback", "escalation"}
    assert lineage_values.isdisjoint(attempt_values)


def test_dropped_attempt_kind_spelling_stores_no_lineage() -> None:
    """The dropped key is ignored; a parent without lineage_kind is refused."""
    terminal = ModelDelegateSkillTerminalProjection.from_payload(
        {
            "status": "completed",
            "task_type": "research",
            "tenant_id": "omninode",
            "correlation_id": str(uuid4()),
            "parent_correlation_id": str(uuid4()),
            "attempt_kind": "escalation",
        }
    )
    folded = HandlerDelegationLineageFold().handle(terminal)
    assert folded.lineage_refusal is not None
    assert folded.row_columns() == {}
