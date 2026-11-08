# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22965: the J12 join (rebuild plan 4.1, piece 12 to piece 13).

A lane returns one typed receipt (its TERMINAL row); the lane result sweep, the receipt classifier,
puts a class row for it in the ledger within 15 minutes. The join is read through the sweep's own
handler over an in-memory ledger, with the overlay's ``join_j12`` block naming the 15 minutes.

Golden: the receipt gets its class row inside 15 minutes and the reading stays WORKING.
Error: a receipt that has gone 16 minutes without a class row flips the reading to NOT WORKING, and
the reading clears when the class row appears.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.models.model_lane_result_sweep import (
    EnumLaneResultClass,
    EnumSweepState,
    ModelLaneResultSweepCommand,
    ModelLaneResultSweepResult,
    ModelReceiptVerdict,
)
from omnimarket.models.model_reading import EnumReadingOutcome, EnumReadingVerdict
from omnimarket.nodes.node_lane_result_sweep_effect.overlay import (
    ModelLaneResultSweepOverlay,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLedgerQuery
from tests.nodes.node_lane_result_sweep_effect.lane_result_sweep_support import (
    BLOCKER,
    handler,
    overlay,
    receipt_key,
    sweep_row,
    terminal,
)

FINISHED = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
CLASS_ROW_SECONDS = 15 * 60
J12 = {"class_row_deadline_seconds": CLASS_ROW_SECONDS}

#: A clean finish and a failed one: both are typed receipts, both get a class row.
RECEIPTS = [
    pytest.param("done", "", id="done"),
    pytest.param("blocked", BLOCKER, id="blocked"),
]


def _command(*, write: bool) -> ModelLaneResultSweepCommand:
    return ModelLaneResultSweepCommand(
        correlation_id="j12-corr", since=FINISHED - timedelta(hours=1), write=write
    )


def _receipt(outcome: str, blocker: str) -> str:
    return terminal("lane-j12", FINISHED, outcome, blocker=blocker)


def _only(result: ModelLaneResultSweepResult) -> ModelReceiptVerdict:
    assert len(result.verdicts) == 1
    return result.verdicts[0]


def _class_rows(ledger: InMemoryLedger) -> list[str]:
    return [line for line in ledger.lines if "action=lane-result-class" in line]


def _class_row_age(ledger: InMemoryLedger, key: str) -> timedelta:
    read = ledger.read(
        ModelLedgerQuery(
            since=FINISHED - timedelta(hours=1),
            until=FINISHED + timedelta(days=1),
            row_type="STATUS",
            term="action=lane-result-class",
        )
    )
    stamps = [row.stamp for row in read.rows if row.cells.get("receipt") == key]
    assert len(stamps) == 1
    return stamps[0] - FINISHED


def _j12_overlay() -> ModelLaneResultSweepOverlay:
    return overlay(join_j12=J12)


# ---- AC1 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("outcome", "blocker"), RECEIPTS)
def test_golden_receipt_gets_a_class_row_within_15_minutes(
    outcome: str, blocker: str
) -> None:
    sweep_at = FINISHED + timedelta(minutes=10)
    sweep, ledger = handler([_receipt(outcome, blocker)], now=sweep_at, join_j12=J12)
    assert _class_rows(ledger) == []

    result = sweep.handle(_command(write=True))

    key = receipt_key("lane-j12", FINISHED)
    assert result.status == "completed"
    assert result.verdict is EnumReadingVerdict.WORKING
    assert result.outcome is EnumReadingOutcome.READ
    assert len(_class_rows(ledger)) == 1
    assert _class_row_age(ledger, key) <= timedelta(seconds=CLASS_ROW_SECONDS)
    assert _only(result).state is not EnumSweepState.UNCLASSIFIED_LATE


def test_golden_class_row_names_the_receipt_and_its_effective_class() -> None:
    sweep, ledger = handler(
        [_receipt("handed-off", BLOCKER)],
        now=FINISHED + timedelta(minutes=5),
        join_j12=J12,
    )
    result = sweep.handle(_command(write=True))

    (row,) = _class_rows(ledger)
    assert f"receipt={receipt_key('lane-j12', FINISHED)}" in row
    assert "receipt_lane=lane-j12" in row
    assert "terminal=handed-off" in row
    assert f"effective={EnumLaneResultClass.BLOCKED.value}" in row
    assert result.verdict is EnumReadingVerdict.WORKING


def test_golden_control_a_second_sweep_adds_no_second_class_row() -> None:
    sweep, ledger = handler(
        [_receipt("done", "")], now=FINISHED + timedelta(minutes=5), join_j12=J12
    )
    sweep.handle(_command(write=True))
    again = sweep.handle(_command(write=True))

    assert len(_class_rows(ledger)) == 1
    assert again.verdict is EnumReadingVerdict.WORKING


# ---- AC2 -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("outcome", "blocker"), RECEIPTS)
def test_error_receipt_withheld_its_class_row_16_minutes_flips_the_reading(
    outcome: str, blocker: str
) -> None:
    sweep, ledger = handler(
        [_receipt(outcome, blocker)],
        now=FINISHED + timedelta(minutes=16),
        join_j12=J12,
    )

    result = sweep.handle(_command(write=False))

    assert _class_rows(ledger) == []
    assert result.verdict is EnumReadingVerdict.NOT_WORKING
    assert _only(result).state is EnumSweepState.UNCLASSIFIED_LATE
    assert result.reasons == ("unclassified-late:lane-j12",)


@pytest.mark.parametrize(("outcome", "blocker"), RECEIPTS)
def test_error_reading_clears_when_the_class_row_appears(
    outcome: str, blocker: str
) -> None:
    key = receipt_key("lane-j12", FINISHED)
    sweep, ledger = handler(
        [_receipt(outcome, blocker)],
        now=FINISHED + timedelta(minutes=16),
        join_j12=J12,
    )
    assert sweep.handle(_command(write=False)).verdict is EnumReadingVerdict.NOT_WORKING

    ledger.lines.append(
        sweep_row(
            FINISHED + timedelta(minutes=16),
            "lane-result-class",
            key,
            f"terminal={outcome}",
        )
    )
    cleared = sweep.handle(_command(write=False))

    assert cleared.verdict is EnumReadingVerdict.WORKING
    assert cleared.reasons == ()
    assert _only(cleared).state is not EnumSweepState.UNCLASSIFIED_LATE


def test_error_a_sweep_that_writes_the_late_class_row_reads_not_working_then_clears() -> (
    None
):
    sweep, ledger = handler(
        [_receipt("done", "")], now=FINISHED + timedelta(minutes=16), join_j12=J12
    )

    late = sweep.handle(_command(write=True))
    assert late.verdict is EnumReadingVerdict.NOT_WORKING
    assert len(_class_rows(ledger)) == 1

    assert sweep.handle(_command(write=True)).verdict is EnumReadingVerdict.WORKING


def test_error_control_15_minutes_is_still_inside_the_window() -> None:
    sweep, _ = handler(
        [_receipt("done", "")], now=FINISHED + timedelta(minutes=15), join_j12=J12
    )
    assert sweep.handle(_command(write=False)).verdict is EnumReadingVerdict.WORKING


def test_error_control_without_the_join_block_the_sweep_reads_as_before() -> None:
    sweep, _ = handler(
        [_receipt("blocked", BLOCKER)], now=FINISHED + timedelta(minutes=16)
    )
    result = sweep.handle(_command(write=False))
    assert result.verdict is EnumReadingVerdict.WORKING
    assert _only(result).state is EnumSweepState.PENDING


def test_overlay_join_block_refuses_a_window_under_a_minute() -> None:
    with pytest.raises(ValidationError, match="class_row_deadline_seconds"):
        overlay(join_j12={"class_row_deadline_seconds": 30})
    assert _j12_overlay().join_j12 is not None
