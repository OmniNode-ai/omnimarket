# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation_events run row carries cross-run lineage (OMN-20287, plan step 1).

Consumer half, in the consumer-first order the wire compatibility gate
enforces (the same order OMN-19860 used for ``caller_lane``):

* one spelling of the lineage vocabulary (``delegation_lineage``);
* the terminal projection model declares ``parent_correlation_id``,
  ``attempt_kind`` and ``parent_failure_cause`` and decodes a malformed value
  as text so it never dead-letters the row;
* a pure fold returns the three columns together or none, with a named
  refusal, never a guessed or partial lineage;
* the sync writer stores them, keeps the run key (one row per correlation id),
  and a lineage-less re-emit leaves a stored lineage untouched.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from omnimarket.models.delegation.delegation_lineage import (
    ATTEMPT_KINDS,
    DELEGATION_ATTEMPT_KIND_METADATA_KEY,
    DELEGATION_PARENT_CORRELATION_METADATA_KEY,
    DELEGATION_PARENT_FAILURE_CAUSE_METADATA_KEY,
    attempt_kind_refusal,
    parent_correlation_refusal,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_lineage_fold import (
    HandlerDelegationLineageFold,
    ModelDelegationLineageFold,
)

from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_BASE: dict[str, object] = {
    "status": "completed",
    "task_type": "research",
    "tenant_id": "omninode",
}


def _terminal(**extra: object) -> ModelDelegateSkillTerminalProjection:
    return ModelDelegateSkillTerminalProjection.from_payload(
        {**_BASE, "correlation_id": str(uuid4()), **extra}
    )


def test_one_spelling_for_the_metadata_keys() -> None:
    assert DELEGATION_PARENT_CORRELATION_METADATA_KEY == "parent_correlation_id"
    assert DELEGATION_ATTEMPT_KIND_METADATA_KEY == "attempt_kind"
    assert DELEGATION_PARENT_FAILURE_CAUSE_METADATA_KEY == "parent_failure_cause"


def test_attempt_kinds_are_the_plan_vocabulary() -> None:
    assert ATTEMPT_KINDS == (
        "first",
        "escalation",
        "engine_fallback",
        "retry",
        "locus_fallback",
    )


@pytest.mark.parametrize("kind", ATTEMPT_KINDS)
def test_every_declared_kind_is_accepted(kind: str) -> None:
    assert attempt_kind_refusal(kind) is None


@pytest.mark.parametrize("kind", ["", "Retry", "fallback", 3, None])
def test_an_undeclared_kind_is_refused_by_name(kind: object) -> None:
    assert attempt_kind_refusal(kind) is not None


def test_a_parent_must_be_a_canonical_uuid() -> None:
    assert parent_correlation_refusal(str(uuid4())) is None
    for bad in ["", "not-a-uuid", str(uuid4()).upper(), 7]:
        assert parent_correlation_refusal(bad) is not None


def test_the_terminal_model_declares_lineage_with_aliases() -> None:
    parent = str(uuid4())
    t = _terminal(
        parentCorrelationId=parent, attemptKind="retry", parentFailureCause="timeout"
    )
    assert t.parent_correlation_id == parent
    assert t.attempt_kind == "retry"
    assert t.parent_failure_cause == "timeout"
    plain = _terminal()
    assert plain.parent_correlation_id is None
    assert plain.attempt_kind is None
    assert plain.parent_failure_cause is None


def test_a_non_string_lineage_value_decodes_as_text() -> None:
    t = _terminal(parent_correlation_id=7, attempt_kind={"k": 1})
    assert t.parent_correlation_id == "7"
    assert t.attempt_kind == '{"k": 1}'


def test_fold_returns_all_three_columns_for_a_linked_run() -> None:
    parent = str(uuid4())
    folded = HandlerDelegationLineageFold().handle(
        _terminal(
            parent_correlation_id=parent,
            attempt_kind="escalation",
            parent_failure_cause="gate_failed",
        )
    )
    assert folded.lineage_refusal is None
    assert folded.row_columns() == {
        "parent_correlation_id": parent,
        "attempt_kind": "escalation",
        "parent_failure_cause": "gate_failed",
    }


def test_fold_names_no_column_for_a_run_with_no_lineage() -> None:
    folded = HandlerDelegationLineageFold().handle(_terminal())
    assert folded.lineage_refusal is None
    assert folded.row_columns() == {}


def test_a_first_attempt_without_a_parent_is_not_lineage() -> None:
    folded = HandlerDelegationLineageFold().handle(_terminal(attempt_kind="first"))
    assert folded.lineage_refusal is None
    assert folded.row_columns() == {"attempt_kind": "first"}


@pytest.mark.parametrize(
    "extra",
    [
        {"attempt_kind": "retry"},
        {"parent_correlation_id": str(uuid4())},
        {"parent_correlation_id": "nope", "attempt_kind": "retry"},
        {"parent_correlation_id": str(uuid4()), "attempt_kind": "mystery"},
        {"parent_failure_cause": "timeout"},
    ],
)
def test_fold_refuses_a_partial_or_malformed_lineage_whole(
    extra: dict[str, object],
) -> None:
    folded = HandlerDelegationLineageFold().handle(_terminal(**extra))
    assert folded.lineage_refusal is not None
    assert folded.row_columns() == {}


def test_a_run_cannot_be_its_own_parent() -> None:
    cid = str(uuid4())
    folded = HandlerDelegationLineageFold().handle(
        ModelDelegateSkillTerminalProjection.from_payload(
            {
                **_BASE,
                "correlation_id": cid,
                "parent_correlation_id": cid,
                "attempt_kind": "retry",
            }
        )
    )
    assert folded.lineage_refusal is not None
    assert folded.row_columns() == {}


def test_fold_model_refuses_columns_and_a_refusal_together() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ModelDelegationLineageFold(attempt_kind="first", lineage_refusal="x")


def _row(db_path: Path, correlation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
        count = conn.execute("SELECT COUNT(*) FROM delegation_events").fetchone()[0]
    finally:
        conn.close()
    assert found is not None
    assert count >= 1
    return dict(found)


def test_sync_writer_stores_lineage_and_a_lineageless_reemit_keeps_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    cid, parent = str(uuid4()), str(uuid4())
    handler = HandlerProjectionDelegation()
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {
                **_BASE,
                "correlation_id": cid,
                "parent_correlation_id": parent,
                "attempt_kind": "locus_fallback",
                "parent_failure_cause": "no_bound_consumer",
            }
        ),
        db,
    )
    row = _row(db_path, cid)
    assert row["parent_correlation_id"] == parent
    assert row["attempt_kind"] == "locus_fallback"
    assert row["parent_failure_cause"] == "no_bound_consumer"
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": cid}
        ),
        db,
    )
    assert _row(db_path, cid)["parent_correlation_id"] == parent


def test_sync_writer_still_writes_the_row_for_a_refused_lineage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    cid = str(uuid4())
    result = HandlerProjectionDelegation().project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": cid, "attempt_kind": "retry"}
        ),
        db,
    )
    assert result.rows_upserted == 1
    row = _row(db_path, cid)
    assert row.get("attempt_kind") is None
    assert row.get("parent_correlation_id") is None


def test_the_migration_adds_the_three_nullable_columns() -> None:
    sql = (
        Path(__file__).parents[3]
        / "src/omnimarket/nodes/node_projection_delegation/migrations"
        / "0055_delegation_events_run_lineage.sql"
    ).read_text()
    for column in ("parent_correlation_id", "attempt_kind", "parent_failure_cause"):
        assert f"ADD COLUMN IF NOT EXISTS {column} TEXT" in sql
    assert "NOT NULL" not in sql
