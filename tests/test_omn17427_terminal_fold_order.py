# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17427: a stored failure terminal is never partly overwritten.

On the .201 dev lane a late infra ``delegation-completed`` arrived after the
delegate-skill failed/cancelled terminal and overwrote the row's score, model and
timestamp while ``operational_outcome`` stayed ``timeout`` or ``cancelled``: a
row that contradicted itself. The fold now decides from each event's own terminal
meaning and time, never from arrival order, and one event owns the whole outcome.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    TABLE,
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    fold_terminal_ownership,
    keep_stored_terminal,
    stored_terminal_owns_outcome,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from tests.test_omn19559_terminal_precedence import (
    CORRELATION_ID,
    MODEL_NAME,
    _inner_completed,
    _outer_terminal,
)

_FAILED_AT = "2026-10-03T01:00:30+00:00"
_EARLIER = "2026-10-03T01:00:00+00:00"
_LATER = "2026-10-03T01:01:00+00:00"


def _at(event: dict[str, object], when: str) -> dict[str, object]:
    return {**event, "timestamp": when}


def _replay(*events: dict[str, object]) -> dict[str, Any]:
    db = InmemoryDatabaseAdapter()
    handler = HandlerProjectionDelegation()
    for event in events:
        assert handler.handle({**event, "_db": db})["rows_upserted"] == 1
    rows = db.query(TABLE)
    assert len(rows) == 1
    return rows[0]


def _assert_whole_failure(row: dict[str, Any], cause: str, outcome: str) -> None:
    """One failure owns every outcome column: nothing from the late completion."""
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == cause
    assert row["operational_outcome"] == outcome
    assert row["content_verdict"] == "not_applicable"
    assert row["quality_gate_passed"] is False
    assert not row.get("model_name")
    assert row["delegated_to"] == "delegate-skill"
    assert not row.get("actual_score")
    assert row["attempt_history"] == []


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cause", "outcome"), [("timeout", "timeout"), ("runtime_shutdown", "cancelled")]
)
def test_failed_then_late_completed_keeps_the_whole_failure(
    cause: str, outcome: str
) -> None:
    failed = _at(_outer_terminal(cause), _FAILED_AT)
    row = _replay(failed, _inner_completed())
    _assert_whole_failure(row, cause, outcome)
    assert str(row["timestamp"]) == _FAILED_AT
    assert not row.get("routed_model")


@pytest.mark.unit
@pytest.mark.parametrize(
    ("cause", "outcome"), [("timeout", "timeout"), ("runtime_shutdown", "cancelled")]
)
def test_completed_then_failed_reads_the_same_whole_failure(
    cause: str, outcome: str
) -> None:
    failed = _at(_outer_terminal(cause), _FAILED_AT)
    row = _replay(_inner_completed(), failed)
    _assert_whole_failure(row, cause, outcome)
    assert str(row["timestamp"]) == _FAILED_AT


@pytest.mark.unit
def test_both_orderings_converge_on_one_outcome() -> None:
    failed = _at(_outer_terminal("timeout"), _FAILED_AT)
    owned = (
        "terminal_ok",
        "terminal_failure_cause",
        "operational_outcome",
        "content_verdict",
        "quality_gate_passed",
        "model_name",
        "delegated_to",
        "actual_score",
        "timestamp",
    )
    forward = _replay(_inner_completed(), failed)
    backward = _replay(failed, _inner_completed())
    assert {c: forward.get(c) for c in owned} == {c: backward.get(c) for c in owned}


@pytest.mark.unit
def test_normal_order_keeps_the_completion_whole() -> None:
    """Positive control: no failure is stored, so the completion is the row."""
    row = _replay(_inner_completed())
    assert row["model_name"] == MODEL_NAME
    assert row["delegated_to"] == MODEL_NAME
    assert row["actual_score"] == pytest.approx(0.98)
    assert row["operational_outcome"] == "completed"
    assert row["content_verdict"] == "usable"
    assert not row.get("terminal_failure_cause")


@pytest.mark.unit
def test_evidenced_success_still_replaces_a_handler_timeout() -> None:
    """Positive control: the OMN-19559 supersession is the one way out of a failure."""
    row = _replay(_outer_terminal("timeout"), _outer_terminal())
    assert row["terminal_ok"] is True
    assert row["model_name"] == MODEL_NAME
    assert row["terminal_failure_cause"] is None
    assert row["operational_outcome"] == "completed"


@pytest.mark.unit
def test_older_failure_arriving_late_does_not_replace_the_newer_one() -> None:
    newer = _at(_outer_terminal("timeout"), _LATER)
    older = _at(_outer_terminal("provider_error"), _EARLIER)
    row = _replay(newer, older)
    assert row["terminal_failure_cause"] == "timeout"
    assert row["operational_outcome"] == "timeout"
    assert str(row["timestamp"]) == _LATER


@pytest.mark.unit
def test_newer_failure_replaces_the_older_one() -> None:
    """Positive control for the timestamp rule: the normal order still moves on."""
    older = _at(_outer_terminal("timeout"), _EARLIER)
    newer = _at(_outer_terminal("provider_error"), _LATER)
    row = _replay(older, newer)
    assert row["terminal_failure_cause"] == "provider_error"
    assert row["operational_outcome"] == "inference_failed"
    assert row["content_verdict"] == "not_applicable"
    assert str(row["timestamp"]) == _LATER


def _stored_failure(**changes: object) -> dict[str, object]:
    return {
        "terminal_ok": False,
        "terminal_failure_cause": "timeout",
        "model_name": "",
        "attempt_history": [],
        "timestamp": datetime(2026, 10, 3, 1, 0, 30, tzinfo=UTC),
        **changes,
    }


@pytest.mark.unit
def test_ownership_for_each_kind_of_incoming_event() -> None:
    stored = _stored_failure()
    late_completion = {"model_name": MODEL_NAME, "actual_score": 1.0}
    assert stored_terminal_owns_outcome(stored, late_completion)
    assert not stored_terminal_owns_outcome({"terminal_ok": True}, late_completion)
    provider_success = {
        "terminal_ok": True,
        "model_name": MODEL_NAME,
        "attempt_history": [{"quality_gate_passed": True, "failure_class": None}],
    }
    assert not stored_terminal_owns_outcome(stored, provider_success)
    assert stored_terminal_owns_outcome(
        _stored_failure(model_name=MODEL_NAME, attempt_history=[{"x": 1}]),
        provider_success,
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    ("incoming", "owned"),
    [
        (datetime(2026, 10, 3, 1, 0, 0, tzinfo=UTC), True),
        ("2026-10-03T01:00:00", True),
        (datetime(2026, 10, 3, 1, 0, 30, tzinfo=UTC), False),
        ("2026-10-03T01:01:00+00:00", False),
        ("not a time", False),
        (None, False),
    ],
)
def test_failure_ordering_reads_each_adapter_time_shape(
    incoming: object, owned: bool
) -> None:
    row = {"terminal_failure_cause": "provider_error", "timestamp": incoming}
    assert stored_terminal_owns_outcome(_stored_failure(), row) is owned


@pytest.mark.unit
def test_failure_over_completion_owns_the_score_even_when_it_states_none() -> None:
    completed = {"terminal_ok": True, "actual_score": 0.98}
    bare: dict[str, object] = {"terminal_failure_cause": "timeout"}
    assert fold_terminal_ownership(completed, bare) is False
    assert bare["actual_score"] is None
    stated: dict[str, object] = {
        "terminal_failure_cause": "timeout",
        "actual_score": 0.0,
    }
    assert fold_terminal_ownership(completed, stated) is False
    assert stated["actual_score"] == 0.0
    completion: dict[str, object] = {"actual_score": 1.0}
    assert fold_terminal_ownership(completed, completion) is True
    assert completion == {"actual_score": 1.0}


@pytest.mark.unit
def test_failure_replacing_failure_states_the_outcome_of_its_own_cause() -> None:
    stored = _stored_failure(
        operational_outcome="timeout", content_verdict="not_applicable"
    )
    newer: dict[str, object] = {
        "terminal_failure_cause": "provider_quota_exhausted",
        "timestamp": datetime(2026, 10, 3, 2, 0, tzinfo=UTC),
    }
    assert fold_terminal_ownership(stored, newer) is True
    assert newer["operational_outcome"] == "provider_quota"
    assert newer["content_verdict"] == "not_applicable"
    named: dict[str, object] = {
        "terminal_failure_cause": "provider_quota_exhausted",
        "operational_outcome": "quality_rejected",
    }
    fold_terminal_ownership(stored, named)
    assert named["operational_outcome"] == "quality_rejected"


@pytest.mark.unit
def test_keep_stored_terminal_drops_only_terminal_owned_columns() -> None:
    row: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "model_name": MODEL_NAME,
        "actual_score": 1.0,
        "timestamp": "2026-10-03T06:00:00+00:00",
        "routed_model": MODEL_NAME,
        "tokens_output": 593,
        "trace_id": "t-1",
    }
    keep_stored_terminal(
        _stored_failure(
            operational_outcome="cancelled", content_verdict="not_applicable"
        ),
        row,
    )
    assert row == {
        "correlation_id": CORRELATION_ID,
        "tokens_output": 593,
        "trace_id": "t-1",
    }


@pytest.mark.unit
def test_keep_stored_terminal_states_the_outcome_a_bare_failure_never_did() -> None:
    row: dict[str, object] = {"operational_outcome": "completed"}
    keep_stored_terminal(
        _stored_failure(terminal_failure_cause="runtime_shutdown"), row
    )
    assert row == {
        "operational_outcome": "cancelled",
        "content_verdict": "not_applicable",
    }


class _StoredRowDb:
    def __init__(self, stored: dict[str, object]) -> None:
        self._stored = stored

    async def execute(self, *_args: object, **_kwargs: object) -> list[dict[str, Any]]:
        return [dict(self._stored)]


def _async_preserve(
    stored: dict[str, object], row: dict[str, object]
) -> dict[str, object]:
    runner = object.__new__(DelegationProjectionRunner)
    runner.__dict__["_db"] = _StoredRowDb(stored)
    runner.__dict__["_table_delegation"] = "delegation_events"
    asyncio.run(runner._preserve_existing_evidence_async(row))
    return row


@pytest.mark.unit
def test_live_async_writer_keeps_a_stored_failure_whole() -> None:
    stored = _stored_failure(
        correlation_id=CORRELATION_ID,
        delegated_to="delegate-skill",
        operational_outcome="timeout",
        content_verdict="not_applicable",
        quality_gate_passed=False,
        actual_score=0.0,
    )
    late_completed: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "model_name": MODEL_NAME,
        "delegated_to": MODEL_NAME,
        "actual_score": 1.0,
        "timestamp": datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
        "operational_outcome": "completed",
        "content_verdict": "usable",
        "quality_gate_passed": True,
        "routed_model": MODEL_NAME,
    }
    row = _async_preserve(stored, late_completed)
    for column in ("model_name", "delegated_to", "timestamp", "routed_model"):
        assert column not in row
    assert "actual_score" not in row
    assert row["terminal_ok"] is False
    assert row["terminal_failure_cause"] == "timeout"
    # The stored failure already states its outcome and verdict, so the row
    # names neither and the upsert leaves them as stored.
    assert "operational_outcome" not in row
    assert "content_verdict" not in row
    assert row["quality_gate_passed"] is False


@pytest.mark.unit
def test_live_async_writer_failure_over_completion_carries_its_own_score() -> None:
    stored = {
        "terminal_ok": True,
        "model_name": MODEL_NAME,
        "actual_score": 0.98,
        "operational_outcome": "completed",
        "content_verdict": "usable",
    }
    failed: dict[str, object] = {
        "correlation_id": CORRELATION_ID,
        "terminal_failure_cause": "provider_error",
        "terminal_ok": False,
        "actual_score": 0.0,
        "model_name": "",
        "attempt_history": [],
        "timestamp": datetime(2026, 10, 3, 6, 0, tzinfo=UTC),
    }
    row = _async_preserve(stored, failed)
    assert row["actual_score"] == 0.0
    assert row["operational_outcome"] == "inference_failed"
    assert SimpleNamespace(**row).timestamp == datetime(2026, 10, 3, 6, 0, tzinfo=UTC)
