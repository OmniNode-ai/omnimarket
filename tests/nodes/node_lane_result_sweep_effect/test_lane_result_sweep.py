# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22967: the sweep classifies a receipt by its result, and holds a class to the one-hour rule."""

from __future__ import annotations

from datetime import timedelta

from omnimarket.models.model_lane_result_sweep import (
    EnumLaneResultClass,
    EnumSweepState,
    ModelLaneResultSweepCommand,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from tests.nodes.node_lane_result_sweep_effect.lane_result_sweep_support import (
    BLOCKER,
    OTHER_BLOCKER,
    SINCE,
    SWEEP_AT,
    class_key_of,
    handler,
    park_row,
    receipt_key,
    sweep_row,
    terminal,
)


def _command(write: bool = False) -> ModelLaneResultSweepCommand:
    return ModelLaneResultSweepCommand(
        correlation_id="test-corr", since=SINCE, write=write
    )


def _only(result):  # type: ignore[no-untyped-def]
    assert len(result.verdicts) == 1
    return result.verdicts[0]


# ---- AC1 -------------------------------------------------------------------------------------


def test_handed_off_but_blocked_is_classified_blocked_not_done() -> None:
    finished = SWEEP_AT - timedelta(minutes=10)
    sweep, _ = handler([terminal("lane-a", finished, "handed-off", blocker=BLOCKER)])
    verdict = _only(sweep.handle(_command()))
    assert verdict.terminal_outcome == "handed-off"
    assert verdict.effective_class is EnumLaneResultClass.BLOCKED
    assert verdict.effective_class is not EnumLaneResultClass.DONE
    assert verdict.class_key == class_key_of(BLOCKER)
    assert verdict.rule_id == "start-criterion-false"


def test_handed_off_but_blocked_by_lane_result_cell_and_engine_rc() -> None:
    finished = SWEEP_AT - timedelta(minutes=10)
    sweep, _ = handler(
        [
            terminal("lane-b", finished, "handed-off", result="blocked"),
            terminal("lane-c", finished, "done", engine_rc=1),
        ]
    )
    classes = {v.lane: v.effective_class for v in sweep.handle(_command()).verdicts}
    assert classes["lane-b"] is EnumLaneResultClass.BLOCKED
    assert classes["lane-c"] is EnumLaneResultClass.FAILED


def test_handed_off_but_blocked_control_a_clean_hand_off_stays_handed_off() -> None:
    finished = SWEEP_AT - timedelta(minutes=10)
    sweep, _ = handler(
        [
            terminal(
                "lane-d", finished, "handed-off", result="handed-off", engine_rc=0
            ),
            terminal("lane-e", finished, "done"),
        ]
    )
    result = sweep.handle(_command())
    by_lane = {v.lane: v for v in result.verdicts}
    assert by_lane["lane-d"].effective_class is EnumLaneResultClass.HANDED_OFF
    assert by_lane["lane-d"].state is EnumSweepState.CLEAR
    assert by_lane["lane-e"].effective_class is EnumLaneResultClass.DONE
    assert result.verdict is EnumReadingVerdict.WORKING


def test_handed_off_but_blocked_writes_class_and_action_rows() -> None:
    finished = SWEEP_AT - timedelta(minutes=10)
    sweep, ledger = handler(
        [terminal("lane-a", finished, "handed-off", blocker=BLOCKER)]
    )
    result = sweep.handle(_command(write=True))
    key = receipt_key("lane-a", finished)
    written = [line for line in ledger.lines if "lane-result-" in line]
    assert any(
        "action=lane-result-class" in line
        and f"receipt={key}" in line
        and "effective=blocked" in line
        and "terminal=handed-off" in line
        for line in written
    )
    assert any(
        "action=lane-result-action" in line
        and f"receipt={key}" in line
        and "rule=start-criterion-false" in line
        for line in written
    )
    assert _only(result).state is EnumSweepState.ACTED
    # Redelivery of the same sweep writes nothing twice.
    again = sweep.handle(_command(write=True))
    assert (
        len([line for line in ledger.lines if "action=lane-result-class" in line]) == 1
    )
    assert _only(again).state is EnumSweepState.ACTED


# ---- AC2 -------------------------------------------------------------------------------------


def test_park_expiry_class_row_without_action_or_park_at_one_hour_fails() -> None:
    finished = SWEEP_AT - timedelta(hours=3)
    key = receipt_key("lane-f", finished)
    classed_at = SWEEP_AT - timedelta(minutes=61)
    sweep, _ = handler(
        [
            terminal("lane-f", finished, "blocked", blocker=OTHER_BLOCKER),
            sweep_row(classed_at, "lane-result-class", key, "effective=blocked"),
        ]
    )
    result = sweep.handle(_command())
    assert _only(result).state is EnumSweepState.NO_ACTION_AT_ONE_HOUR
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any("no-action-at-one-hour" in r for r in result.reasons)


def test_park_expiry_control_inside_the_hour_is_pending_not_failed() -> None:
    finished = SWEEP_AT - timedelta(minutes=30)
    key = receipt_key("lane-f", finished)
    sweep, _ = handler(
        [
            terminal("lane-f", finished, "blocked", blocker=OTHER_BLOCKER),
            sweep_row(SWEEP_AT - timedelta(minutes=20), "lane-result-class", key),
        ]
    )
    result = sweep.handle(_command())
    assert _only(result).state is EnumSweepState.PENDING
    assert result.verdict is EnumReadingVerdict.WORKING


def test_park_expiry_valid_park_satisfies_the_rule() -> None:
    finished = SWEEP_AT - timedelta(hours=3)
    key = receipt_key("lane-f", finished)
    sweep, _ = handler(
        [
            terminal("lane-f", finished, "blocked", blocker=OTHER_BLOCKER),
            sweep_row(SWEEP_AT - timedelta(hours=2), "lane-result-class", key),
            park_row(
                SWEEP_AT - timedelta(hours=1),
                key,
                cite="OMN-22478",
                review_by=SWEEP_AT + timedelta(hours=4),
            ),
        ]
    )
    result = sweep.handle(_command())
    assert _only(result).state is EnumSweepState.PARKED
    assert result.verdict is EnumReadingVerdict.WORKING


def test_park_expiry_park_past_review_by_reads_not_working() -> None:
    finished = SWEEP_AT - timedelta(hours=3)
    key = receipt_key("lane-f", finished)
    sweep, _ = handler(
        [
            terminal("lane-f", finished, "blocked", blocker=OTHER_BLOCKER),
            sweep_row(SWEEP_AT - timedelta(hours=2), "lane-result-class", key),
            park_row(
                SWEEP_AT - timedelta(hours=1),
                key,
                cite="OMN-22478",
                review_by=SWEEP_AT - timedelta(minutes=1),
            ),
        ]
    )
    result = sweep.handle(_command())
    assert _only(result).state is EnumSweepState.PARK_EXPIRED
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert any("park-expired" in r for r in result.reasons)


def test_park_expiry_park_without_a_citation_is_not_a_valid_park() -> None:
    finished = SWEEP_AT - timedelta(hours=3)
    key = receipt_key("lane-f", finished)
    sweep, _ = handler(
        [
            terminal("lane-f", finished, "blocked", blocker=OTHER_BLOCKER),
            sweep_row(SWEEP_AT - timedelta(hours=2), "lane-result-class", key),
            park_row(
                SWEEP_AT - timedelta(hours=1),
                key,
                cite="",
                review_by=SWEEP_AT + timedelta(hours=4),
            ),
        ]
    )
    assert _only(sweep.handle(_command())).state is EnumSweepState.NO_ACTION_AT_ONE_HOUR


def test_park_expiry_action_row_satisfies_the_rule_and_a_late_class_row_fails() -> None:
    acted = SWEEP_AT - timedelta(hours=3)
    late = SWEEP_AT - timedelta(hours=2)
    acted_key = receipt_key("lane-g", acted)
    sweep, _ = handler(
        [
            terminal("lane-g", acted, "blocked", blocker=BLOCKER),
            sweep_row(SWEEP_AT - timedelta(hours=2), "lane-result-class", acted_key),
            sweep_row(SWEEP_AT - timedelta(hours=2), "lane-result-action", acted_key),
            terminal("lane-h", late, "blocked", blocker=OTHER_BLOCKER),
        ]
    )
    by_lane = {v.lane: v for v in sweep.handle(_command()).verdicts}
    assert by_lane["lane-g"].state is EnumSweepState.ACTED
    assert by_lane["lane-h"].state is EnumSweepState.UNCLASSIFIED_LATE


def test_park_expiry_unreadable_ledger_is_source_unreadable_not_working() -> None:
    sweep, ledger = handler([])
    ledger.read_error = "ledger route down"
    result = sweep.handle(_command())
    assert result.status == "completed"
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert result.outcome is EnumReadingOutcome.SOURCE_UNREADABLE


def test_park_expiry_unmatched_class_writes_class_row_only() -> None:
    finished = SWEEP_AT - timedelta(minutes=5)
    sweep, ledger = handler(
        [terminal("lane-i", finished, "blocked", blocker=OTHER_BLOCKER)]
    )
    result = sweep.handle(_command(write=True))
    assert _only(result).rule_id == ""
    assert _only(result).state is EnumSweepState.PENDING
    assert any(
        "action=lane-result-class" in line and "rule=none" in line
        for line in ledger.lines
    )
    assert not any("action=lane-result-action" in line for line in ledger.lines)


def test_cli_without_an_overlay_exits_3_naming_the_fact(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from omnimarket.nodes.node_lane_result_sweep_effect.runtime_lane_result_sweep import (
        main,
    )

    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    assert main(["probe", "--since", "2026-11-07T00:00:00Z"]) == 3
    assert "INDETERMINATE" in capsys.readouterr().err


def test_handed_off_but_blocked_landing_controller_result_vocabulary() -> None:
    """The shape 13,826 hidden failures took: outcome=handed-off, the failure in ``result=``."""
    finished = SWEEP_AT - timedelta(minutes=10)
    lines = [
        terminal(
            "lane-j", finished, "handed-off", result="external_blocker", engine_rc=0
        ),
        terminal(
            "lane-k", finished, "handed-off", result="no_result_file", engine_rc=86
        ),
        terminal("lane-l", finished, "handed-off", result="fix_submitted", engine_rc=0),
        terminal("lane-m", finished, "handed-off", result="a_word_nobody_listed"),
    ]
    sweep, _ = handler(lines)
    by_lane = {v.lane: v for v in sweep.handle(_command()).verdicts}
    assert by_lane["lane-j"].effective_class is EnumLaneResultClass.BLOCKED
    assert by_lane["lane-k"].effective_class is EnumLaneResultClass.FAILED
    assert by_lane["lane-l"].effective_class is EnumLaneResultClass.HANDED_OFF
    # An unlisted word shows as unknown; it is never counted as a clean finish.
    assert by_lane["lane-m"].effective_class is EnumLaneResultClass.UNKNOWN
    assert by_lane["lane-m"].state is EnumSweepState.PENDING


def test_park_expiry_an_append_that_does_not_land_fails_the_run_and_writes_no_reading() -> (
    None
):
    finished = SWEEP_AT - timedelta(minutes=5)
    sweep, ledger = handler([terminal("lane-n", finished, "blocked", blocker=BLOCKER)])
    ledger.append_status = "pending"
    result = sweep.handle(_command(write=True))
    assert result.status == "failed"
    assert result.failure == "append-failed:pending"
    assert result.verdict is None
    assert not any("action=lane-result-reading" in line for line in ledger.lines)
