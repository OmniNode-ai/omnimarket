# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J7 runner report (OMN-23717): the reason and the outcome reach the publisher's step log.

The publisher keeps only the stderr tail of a failed step, and the runner wrote its result JSON to stdout alone, so the log
line of a J7 failure was empty. The runner now also writes one ``JOIN-J7-FAILED`` line to stderr; the JSON and the exit code
stay.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.models.model_lab_check_run_join_j7_run import (
    EnumJoinJ7RunOutcome,
    ModelJoinJ7RunItem,
    ModelJoinJ7RunResult,
)
from omnimarket.nodes.node_lab_check_run_effect.runtime_join_j7 import (
    emit,
    failure_report,
)

pytestmark = pytest.mark.unit

CID = "0f8b6c1e2d3a4b5c8d9e0a1b2c3d4e5f"


def _landed() -> ModelJoinJ7RunResult:
    return ModelJoinJ7RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ7RunItem(correlation_id=CID, outcome=EnumJoinJ7RunOutcome.LANDED),
        ),
    )


def _read_failed() -> ModelJoinJ7RunResult:
    return ModelJoinJ7RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ7RunItem(
                correlation_id=CID,
                outcome=EnumJoinJ7RunOutcome.HEAD_NOT_PASSING,
                failure_code="head_already_passing",
                detail="the head already reads pass before the post",
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
        ModelJoinJ7RunResult(read_ok=False, reason="ledger unread: read-command-exit-1")
    )

    assert (
        report
        == "JOIN-J7-FAILED read_ok=false reason=ledger unread: read-command-exit-1"
    )


def test_error_a_read_failure_reaches_stderr_as_one_line_and_exit_one(capsys) -> None:
    assert emit(_read_failed()) == 1

    out = capsys.readouterr()
    assert json.loads(out.out)["items"][0]["outcome"] == "head_not_passing"
    lines = out.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("JOIN-J7-FAILED ")
    for part in (
        CID,
        "head_already_passing",
        "the head already reads pass before the post",
    ):
        assert part in lines[0]


def test_control_a_landed_and_a_drill_item_beside_a_failed_one_are_not_named() -> None:
    mixed = ModelJoinJ7RunResult(
        read_ok=True,
        owed=2,
        items=(
            *_landed().items,
            ModelJoinJ7RunItem(
                correlation_id=CID,
                outcome=EnumJoinJ7RunOutcome.DRILL_RECORDED,
                drill=True,
                failure_code="superseded_attempt_refused",
                detail="the superseded attempt's post was refused and recorded",
            ),
            *_read_failed().items,
        ),
    )

    report = failure_report(mixed)

    assert report is not None
    assert report.count(CID) == 1
    assert "landed" not in report
    assert "drill_recorded" not in report
