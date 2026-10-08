# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A fallback delegation names the delegation it follows (OMN-20606).

Measured on the h201 dev lane on 2026-10-05: 93 of 94 failed delegations that
carried a session were answered by a later in-process delegation within 120 s,
and the answering row had a new correlation id, an empty caller_lane and
nothing naming the failed parent. Two defects, both proved here:

* nothing carried lineage at all: the request, the terminal and the row had no
  field for the parent, the kind of relation or the parent's failure cause;
* the in-process port's evidence terminal, the only terminal that reaches the
  bus on that path, never copied the request's caller lane or ticket, although
  the handler's own terminal carries both.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from omnimarket.enums.enum_delegation_lineage_kind import EnumDelegationLineageKind
from omnimarket.models.delegation.delegation_lineage import (
    LINEAGE_KEYS,
    ModelDelegationLineage,
    resolve_lineage,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillCompleted,
    ModelDelegateSkillFailed,
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation_lineage_fold import (
    HandlerDelegationLineageFold,
    ModelDelegationLineageFold,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_PARENT = "5b0c8a52-9a43-4f3c-a9f1-0d6f2b4e7c11"
_LANE = "deleg-fallback-mark-9143"
_LINEAGE: dict[str, object] = {
    "parent_correlation_id": _PARENT,
    "lineage_kind": "fallback",
    "parent_failure_cause": "provider_quota_exhausted",
}
_BASE: dict[str, object] = {
    "status": "completed",
    "task_type": "document",
    "tenant_id": "omninode",
}


def _terminal(**extra: object) -> ModelDelegateSkillTerminalProjection:
    return ModelDelegateSkillTerminalProjection.from_payload(
        {**_BASE, "correlation_id": str(uuid4()), **extra}
    )


# ---------------------------------------------------------------- the one spelling


def test_the_three_keys_are_the_metadata_terminal_and_column_names() -> None:
    assert (
        frozenset({"parent_correlation_id", "lineage_kind", "parent_failure_cause"})
        == LINEAGE_KEYS
    )
    assert {k.value for k in EnumDelegationLineageKind} == {"fallback", "escalation"}


def test_a_whole_lineage_resolves_and_round_trips_as_columns() -> None:
    lineage, refusal = resolve_lineage(_LINEAGE)
    assert refusal is None
    assert lineage == ModelDelegationLineage(
        parent_correlation_id=UUID(_PARENT),
        lineage_kind=EnumDelegationLineageKind.FALLBACK,
        parent_failure_cause="provider_quota_exhausted",
    )
    assert lineage.as_columns() == _LINEAGE


def test_the_cause_is_optional() -> None:
    lineage, refusal = resolve_lineage(
        {"parent_correlation_id": _PARENT, "lineage_kind": "escalation"}
    )
    assert refusal is None
    assert lineage is not None
    assert lineage.as_columns() == {
        "parent_correlation_id": _PARENT,
        "lineage_kind": "escalation",
    }


def test_no_lineage_key_is_no_lineage_and_no_refusal() -> None:
    assert resolve_lineage({"caller_lane": _LANE}) == (None, None)


@pytest.mark.parametrize(
    ("values", "fragment"),
    [
        ({"lineage_kind": "fallback"}, "together"),
        ({"parent_correlation_id": _PARENT}, "together"),
        ({"parent_failure_cause": "exit_124"}, "together"),
        ({**_LINEAGE, "parent_correlation_id": "not-a-uuid"}, "is not a UUID"),
        ({**_LINEAGE, "parent_correlation_id": 17}, "is not a string"),
        ({**_LINEAGE, "lineage_kind": "retry"}, "is not one of fallback, escalation"),
        ({**_LINEAGE, "parent_failure_cause": "Two Words"}, "does not match"),
        ({**_LINEAGE, "parent_failure_cause": "x" * 65}, "does not match"),
    ],
)
def test_a_partial_or_malformed_lineage_is_refused_by_name(
    values: dict[str, object], fragment: str
) -> None:
    lineage, refusal = resolve_lineage(values)
    assert lineage is None
    assert refusal is not None
    assert fragment in refusal


def test_a_delegation_cannot_follow_itself() -> None:
    lineage, refusal = resolve_lineage(_LINEAGE, own_correlation_id=_PARENT)
    assert lineage is None
    assert refusal is not None
    assert "cannot follow itself" in refusal


# ------------------------------------------------------------ the terminal models


def test_the_projection_model_decodes_lineage_and_its_aliases() -> None:
    terminal = _terminal(
        parentCorrelationId=_PARENT, lineageKind="fallback", parentFailureCause="x"
    )
    assert terminal.parent_correlation_id == _PARENT
    assert terminal.lineage_kind == "fallback"
    assert terminal.parent_failure_cause == "x"
    assert _terminal().lineage_kind is None


def test_a_non_string_lineage_value_does_not_fail_terminal_decoding() -> None:
    """Lineage is attribution: it must never dead-letter the delegation's row."""
    assert _terminal(parent_correlation_id=17).parent_correlation_id == "17"


@pytest.mark.parametrize(
    "model", [ModelDelegateSkillResponse, ModelDelegateSkillCompleted]
)
def test_the_response_tolerates_lineage_before_declaring_it(model: type) -> None:
    """Step 1 of the OMN-18868 order: decode, and drop, before declaring."""
    decoded = model.model_validate(
        {
            "status": "completed",
            "task_type": "document",
            "quality_gate_passed": True,
            "correlation_id": str(uuid4()),
            **_LINEAGE,
        }
    )
    assert not LINEAGE_KEYS & set(type(decoded).model_fields)
    assert not LINEAGE_KEYS & set(decoded.model_dump())


def test_the_failed_variant_tolerates_lineage_too() -> None:
    ModelDelegateSkillFailed.model_validate(
        {
            "status": "failed",
            "task_type": "document",
            "quality_gate_passed": False,
            "correlation_id": str(uuid4()),
            **_LINEAGE,
        }
    )


def test_the_response_still_refuses_an_unknown_key() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        ModelDelegateSkillResponse.model_validate(
            {
                "status": "completed",
                "task_type": "document",
                "correlation_id": str(uuid4()),
                **_LINEAGE,
                "not_a_field": "x",
            }
        )


def test_the_projection_model_keeps_what_the_response_drops() -> None:
    assert set(ModelDelegateSkillTerminalProjection.model_fields) >= LINEAGE_KEYS


# ---------------------------------------------------------------------- the fold


def test_lineage_fold_returns_the_three_columns() -> None:
    folded = HandlerDelegationLineageFold().handle(_terminal(**_LINEAGE))
    assert folded.lineage_refusal is None
    assert folded.row_columns() == _LINEAGE


def test_lineage_fold_names_no_column_without_lineage() -> None:
    folded = HandlerDelegationLineageFold().handle(_terminal())
    assert folded == ModelDelegationLineageFold()
    assert folded.row_columns() == {}


def test_lineage_fold_refuses_a_malformed_lineage_and_names_no_column() -> None:
    folded = HandlerDelegationLineageFold().handle(
        _terminal(**{**_LINEAGE, "lineage_kind": "retry"})
    )
    assert folded.lineage is None
    assert folded.lineage_refusal is not None
    assert folded.row_columns() == {}


def test_lineage_fold_model_refuses_both_outcomes_together() -> None:
    lineage, _ = resolve_lineage(_LINEAGE)
    with pytest.raises(ValueError, match="exactly one"):
        ModelDelegationLineageFold(lineage=lineage, lineage_refusal="x")


# ------------------------------------------------------------------- the writer


def _row(db_path: Path, correlation_id: str) -> dict[str, Any]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT * FROM delegation_events WHERE correlation_id = ?",
            (correlation_id,),
        ).fetchone()
    finally:
        conn.close()
    assert found is not None
    return dict(found)


def test_lineage_sync_writer_stores_it_and_a_lineage_less_reemit_keeps_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    correlation_id = str(uuid4())
    handler = HandlerProjectionDelegation()
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": correlation_id, **_LINEAGE}
        ),
        db,
    )
    row = _row(db_path, correlation_id)
    for key, value in _LINEAGE.items():
        assert row[key] == value
    handler.project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {**_BASE, "correlation_id": correlation_id}
        ),
        db,
    )
    assert _row(db_path, correlation_id)["parent_correlation_id"] == _PARENT


def test_lineage_sync_writer_still_writes_the_row_for_a_malformed_lineage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "delegation.sqlite"
    db = SqliteDatabaseAdapter(db_path)
    correlation_id = str(uuid4())
    result = HandlerProjectionDelegation().project_delegate_skill_terminal(
        ModelDelegateSkillTerminalProjection.from_payload(
            {
                **_BASE,
                "correlation_id": correlation_id,
                **_LINEAGE,
                "parent_correlation_id": "not-a-uuid",
            }
        ),
        db,
    )
    assert result.rows_upserted == 1
    row = _row(db_path, correlation_id)
    assert row["task_type"] == "document"
    assert row.get("parent_correlation_id") is None
    assert row.get("lineage_kind") is None
