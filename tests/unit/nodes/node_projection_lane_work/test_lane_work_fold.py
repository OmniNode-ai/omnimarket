# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold of lane work receipts into one row per workflow (OMN-26708)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from omnibase_core.enums.enum_lane_work_state import EnumLaneWorkState

from omnimarket.nodes.node_projection_lane_work.handlers.handler_projection_lane_work import (
    HandlerProjectionLaneWork,
)
from omnimarket.nodes.node_projection_lane_work.handlers.lane_work_fold import (
    fold_receipts,
    terminal_rows,
)
from tests.unit.nodes.node_projection_lane_work.receipts import (
    T0,
    E,
    S,
    blocked_workflow,
    handed_off_workflow,
    live_workflow,
    receipt,
)

pytestmark = pytest.mark.unit


def test_folding_receipts_gives_one_row_per_workflow_with_its_current_state() -> None:
    receipts = [*live_workflow("acme-repo-1-1"), *handed_off_workflow("acme-repo-2-1")]
    rows = fold_receipts(receipts)
    assert sorted(rows) == ["acme-repo-1-1", "acme-repo-2-1"]
    assert rows["acme-repo-1-1"].state is S.CLAIMED
    assert rows["acme-repo-1-1"].seq == 3
    assert rows["acme-repo-1-1"].terminal_kind is None
    assert rows["acme-repo-1-1"].ended_at is None


def test_an_ended_workflow_carries_its_terminal_fields() -> None:
    rows = fold_receipts(handed_off_workflow("acme-repo-2-1"))
    row = rows["acme-repo-2-1"]
    assert row.state is S.HANDED_OFF
    assert row.terminal_kind == "handed_off"
    assert row.ended_at == T0 + timedelta(minutes=25)
    blocked = fold_receipts(blocked_workflow("acme-repo-3-1"))["acme-repo-3-1"]
    assert blocked.terminal_kind == "blocked"


def test_ac2_replaying_the_transition_topic_twice_gives_the_same_table() -> None:
    receipts = [
        *live_workflow("a-1-1"),
        *handed_off_workflow("b-2-1"),
        *blocked_workflow("c-3-1"),
    ]
    once = fold_receipts(receipts)
    twice = fold_receipts([*receipts, *receipts])
    assert twice == once
    assert once["b-2-1"].transitions_taken == 8
    assert once["b-2-1"].help_request_count == 1


def test_ac3_duration_and_help_counts_are_derived_from_receipt_timestamps() -> None:
    rows = fold_receipts([*handed_off_workflow("b-2-1"), *live_workflow("a-1-1")])
    ended = rows["b-2-1"]
    assert ended.started_at == T0
    assert ended.duration_seconds == 25 * 60
    assert ended.help_request_count == 1
    live = rows["a-1-1"]
    assert live.duration_seconds == 3 * 60
    assert live.help_request_count == 0
    for row in rows.values():
        assert row.duration_seconds is not None
        assert row.help_request_count is not None
        assert row.transitions_taken is not None


def test_a_refused_receipt_advances_seq_but_makes_no_transition() -> None:
    receipts = [
        *live_workflow("a-1-1"),
        receipt(
            "a-1-1",
            4,
            E.CHECK_GREEN,
            S.CLAIMED,
            S.CLAIMED,
            minutes=4,
            kind="refused",
            refusal="no such edge",
        ),
    ]
    row = fold_receipts(receipts)["a-1-1"]
    assert row.seq == 4
    assert row.state is S.CLAIMED
    assert row.transitions_taken == 3


def test_a_receipt_older_than_the_row_is_refused() -> None:
    receipts = handed_off_workflow("b-2-1")
    reordered = [receipts[7], *receipts[:7]]
    assert fold_receipts(reordered)["b-2-1"].seq == 8
    assert fold_receipts(reordered)["b-2-1"].state is S.HANDED_OFF


def test_ac4_the_terminal_rows_query_returns_only_ended_workflows() -> None:
    rows = fold_receipts(
        [
            *live_workflow("a-1-1"),
            *handed_off_workflow("b-2-1"),
            *blocked_workflow("c-3-1"),
        ]
    )
    ended = terminal_rows(rows)
    assert sorted(row.workflow_id for row in ended) == ["b-2-1", "c-3-1"]
    assert all(row.state in (S.HANDED_OFF, S.BLOCKED) for row in ended)


def test_the_pure_handler_folds_one_receipt_into_its_row() -> None:
    result = HandlerProjectionLaneWork().handle(handed_off_workflow("b-2-1")[-1])
    assert result.applied is True
    assert len(result.rows) == 1
    assert result.rows[0]["workflow_id"] == "b-2-1"
    assert result.rows[0]["state"] == EnumLaneWorkState.HANDED_OFF.value
