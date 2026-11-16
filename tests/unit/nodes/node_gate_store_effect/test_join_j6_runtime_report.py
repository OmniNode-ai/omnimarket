# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J6 runner report (OMN-23717): the reason and the outcome reach the publisher's step log.

The publisher keeps only the stderr tail of a failed step, and the runner wrote its result JSON to stdout alone, so the
log line of a J6 failure was empty. The runner now also writes one ``JOIN-J6-FAILED`` line to stderr, the form the J5
runner (``runtime_join_j5``) already uses. The JSON and the exit code stay.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.models.model_gate_store_join_j6_run import (
    EnumJoinJ6RunOutcome,
    ModelJoinJ6RunItem,
    ModelJoinJ6RunResult,
)
from omnimarket.nodes.node_gate_store_effect.runtime_join_j6 import (
    emit,
    failure_report,
)

pytestmark = pytest.mark.unit

CID = "0f8b6c1e2d3a4b5c8d9e0a1b2c3d4e5f"
CID_DRILL = "1a2b3c4d5e6f708192a3b4c5d6e7f809"


def _landed() -> ModelJoinJ6RunResult:
    return ModelJoinJ6RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ6RunItem(
                correlation_id=CID,
                outcome=EnumJoinJ6RunOutcome.LANDED,
            ),
        ),
    )


def _drill_recorded() -> ModelJoinJ6RunResult:
    return ModelJoinJ6RunResult(
        read_ok=True,
        owed=0,
        items=(
            ModelJoinJ6RunItem(
                correlation_id=CID_DRILL,
                outcome=EnumJoinJ6RunOutcome.DRILL_RECORDED,
                drill=True,
                failure_code="fence_closed_stop_unanswered",
                detail="no answer within 0.05 seconds",
            ),
        ),
    )


def _failed() -> ModelJoinJ6RunResult:
    return ModelJoinJ6RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ6RunItem(
                correlation_id=CID,
                outcome=EnumJoinJ6RunOutcome.STOP_NOT_COMMITTED,
                failure_code="audit_row_missing",
                detail="the audit row was not written: read-command-exit-1",
            ),
        ),
    )


def test_golden_a_landed_run_has_no_failure_line_and_exits_zero(capsys) -> None:
    assert failure_report(_landed()) is None

    assert emit(_landed()) == 0

    out = capsys.readouterr()
    assert json.loads(out.out)["items"][0]["outcome"] == "landed"
    assert out.err == ""


def test_error_an_unreadable_ledger_names_its_reason() -> None:
    report = failure_report(
        ModelJoinJ6RunResult(read_ok=False, reason="ledger unread: read-command-exit-1")
    )

    assert (
        report
        == "JOIN-J6-FAILED read_ok=false reason=ledger unread: read-command-exit-1"
    )


def test_error_a_failed_item_reaches_stderr_as_one_line_and_exit_one(capsys) -> None:
    assert emit(_failed()) == 1

    out = capsys.readouterr()
    assert json.loads(out.out)["items"][0]["outcome"] == "stop_not_committed"
    lines = out.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("JOIN-J6-FAILED ")
    for part in (CID, "audit_row_missing", "read-command-exit-1"):
        assert part in lines[0]


def test_control_a_landed_and_drill_recorded_item_beside_a_failed_one_are_not_named() -> (
    None
):
    mixed = ModelJoinJ6RunResult(
        read_ok=True,
        owed=2,
        items=(*_landed().items, *_drill_recorded().items, *_failed().items),
    )

    report = failure_report(mixed)

    assert report is not None
    assert report.count(CID) == 1
    assert "landed" not in report
    assert "drill_recorded" not in report
    assert CID_DRILL not in report
