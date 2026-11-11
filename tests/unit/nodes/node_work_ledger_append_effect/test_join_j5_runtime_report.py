# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 runner report (OMN-23717): the reason and the outcome reach the publisher's step log.

The publisher keeps only the stderr tail of a failed step, and the runner wrote its result JSON to stdout alone, so the log
line of a J5 failure was empty. The runner now also writes one ``JOIN-J5-FAILED`` line to stderr, the form
``runtime_overlay_drift`` (join J3) already uses (``OVERLAY-DRIFT-FAILED <reason>``). The JSON and the exit code stay.
"""

from __future__ import annotations

import json

import pytest

from omnimarket.models.work_ledger_append import (
    EnumJoinJ5Outcome,
    ModelJoinJ5RunItem,
    ModelJoinJ5RunResult,
)
from omnimarket.nodes.node_work_ledger_append_effect.runtime_join_j5 import (
    emit,
    failure_report,
)

pytestmark = pytest.mark.unit

CID = "0f8b6c1e2d3a4b5c8d9e0a1b2c3d4e5f"


def _landed() -> ModelJoinJ5RunResult:
    return ModelJoinJ5RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ5RunItem(
                correlation_id=CID, outcome=EnumJoinJ5Outcome.LANDED, ledger_seq=3
            ),
        ),
    )


def _read_failed() -> ModelJoinJ5RunResult:
    return ModelJoinJ5RunResult(
        read_ok=True,
        owed=1,
        items=(
            ModelJoinJ5RunItem(
                correlation_id=CID,
                outcome=EnumJoinJ5Outcome.PROJECTION_READ_FAILED,
                failure_code="projection_read_failed",
                detail="the projection read failed on its last of 6 attempts: read-command-exit-1",
                ledger_seq=3,
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
        ModelJoinJ5RunResult(read_ok=False, reason="ledger unread: read-command-exit-1")
    )

    assert (
        report
        == "JOIN-J5-FAILED read_ok=false reason=ledger unread: read-command-exit-1"
    )


def test_error_a_read_failure_reaches_stderr_as_one_line_and_exit_one(capsys) -> None:
    assert emit(_read_failed()) == 1

    out = capsys.readouterr()
    assert json.loads(out.out)["items"][0]["outcome"] == "projection_read_failed"
    lines = out.err.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("JOIN-J5-FAILED ")
    for part in (CID, "projection_read_failed", "read-command-exit-1"):
        assert part in lines[0]


def test_control_a_landed_item_beside_a_failed_one_is_not_named() -> None:
    mixed = ModelJoinJ5RunResult(
        read_ok=True,
        owed=2,
        items=(*_landed().items, *_read_failed().items),
    )

    report = failure_report(mixed)

    assert report is not None
    assert report.count(CID) == 1
    assert "landed" not in report
