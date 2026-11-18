# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23153: the exact UTC daily receipt compares the ledger table with the accepted-row topic only.

AC1: equal data reports missing, extra and mismatched as 0 and lists both sides.
AC2: one row planted in the projection alone is one extra and reads red (today's defect).
AC3: the receipt carries the verb comparison, and runs at the contract's fixed time with a 10 minute close.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.work_ledger_daily_receipt import (
    build_daily_receipt,
    daily_receipt_status_row,
    load_daily_receipt_config,
    receipt_window,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_daily_receipt import (
    EnumDailyReceiptState,
    ModelDailyReceiptRequest,
    ModelDailyReceiptRow,
)

pytestmark = pytest.mark.unit

DAY = "2026-11-12"
CONFIG = load_daily_receipt_config()
_, CUTOFF, CLOSES = receipt_window(DAY, CONFIG)
AFTER_CLOSE = CLOSES + timedelta(minutes=1)


def _row(
    n: int, *, seq: int | None = None, seen: datetime | None = None
) -> ModelDailyReceiptRow:
    return ModelDailyReceiptRow(
        row_id=f"{n:064x}",
        ledger_seq=seq if seq is not None else n,
        row_ts=datetime(2026, 11, 12, 10, n % 60, tzinfo=UTC),
        seen_at=seen,
    )


def _request(
    table: list[ModelDailyReceiptRow],
    topic: list[ModelDailyReceiptRow],
    *,
    now: datetime = AFTER_CLOSE,
    verb_diff: dict[str, int | None] | None = None,
) -> ModelDailyReceiptRequest:
    return ModelDailyReceiptRequest(
        day=DAY,
        now=now,
        table_rows=tuple(table),
        topic_rows=tuple(topic),
        verb_diff=verb_diff,
    )


def test_daily_receipt_exact_equal_data_reports_zero_and_lists_both_sides() -> None:
    rows = [_row(n) for n in range(1, 6)]
    receipt = build_daily_receipt(_request(rows, list(reversed(rows))), CONFIG)
    assert (receipt.missing, receipt.extra, receipt.mismatched) == (0, 0, 0)
    assert receipt.state is EnumDailyReceiptState.GREEN
    assert receipt.exact is True
    assert (
        receipt.table_row_ids == receipt.topic_row_ids == tuple(r.row_id for r in rows)
    )


def test_daily_receipt_exact_a_row_missing_from_the_table_is_missing() -> None:
    rows = [_row(n) for n in range(1, 6)]
    receipt = build_daily_receipt(_request(rows[:-1], rows), CONFIG)
    assert receipt.missing_row_ids == (rows[-1].row_id,)
    assert (receipt.missing, receipt.extra) == (1, 0)
    assert receipt.state is EnumDailyReceiptState.RED


def test_daily_receipt_exact_a_row_under_two_sequences_is_mismatched() -> None:
    rows = [_row(n) for n in range(1, 4)]
    moved = [*rows[:-1], _row(3, seq=99)]
    receipt = build_daily_receipt(_request(moved, rows), CONFIG)
    assert receipt.mismatched_row_ids == (rows[-1].row_id,)
    assert (receipt.missing, receipt.extra, receipt.mismatched) == (0, 0, 1)
    assert receipt.state is EnumDailyReceiptState.RED


def test_daily_receipt_exact_empty_day_is_not_exact_and_duplicates_follow_the_key_rule() -> (
    None
):
    assert (
        build_daily_receipt(_request([], []), CONFIG).state is EnumDailyReceiptState.RED
    )
    rows = [_row(n) for n in range(1, 4)]
    twice = [*rows, rows[0]]  # an identical duplicate is dropped by key
    assert build_daily_receipt(_request(twice, rows), CONFIG).exact is True
    conflicting = [
        *rows,
        _row(1, seq=50),
    ]  # one key under two sequences fails the window
    receipt = build_daily_receipt(_request(conflicting, rows), CONFIG)
    assert receipt.conflicting_row_ids == (rows[0].row_id,)
    assert receipt.state is EnumDailyReceiptState.RED


def test_daily_receipt_planted_projection_row_reads_one_extra_and_red() -> None:
    rows = [_row(n) for n in range(1, 6)]
    control = build_daily_receipt(_request(rows, rows), CONFIG)
    assert (
        control.state is EnumDailyReceiptState.GREEN
    )  # positive control: no plant, no red
    planted = _row(77, seq=7777)
    receipt = build_daily_receipt(_request([*rows, planted], rows), CONFIG)
    assert receipt.extra == 1
    assert receipt.extra_row_ids == (planted.row_id,)
    assert (receipt.missing, receipt.mismatched) == (0, 0)
    assert receipt.state is EnumDailyReceiptState.RED
    assert receipt.exact is False
    assert "| extra=1 |" in daily_receipt_status_row(receipt, CONFIG, AFTER_CLOSE)


def test_daily_receipt_planted_projection_row_after_the_close_is_late_not_extra() -> (
    None
):
    rows = [_row(n) for n in range(1, 4)]
    late_plant = _row(77, seen=CLOSES + timedelta(minutes=5))
    receipt = build_daily_receipt(_request([*rows, late_plant], rows), CONFIG)
    assert (receipt.extra, receipt.late_rows) == (0, 1)
    assert receipt.state is EnumDailyReceiptState.GREEN


def test_daily_receipt_planted_projection_row_other_days_are_not_in_the_window() -> (
    None
):
    rows = [_row(n) for n in range(1, 4)]
    other = ModelDailyReceiptRow(
        row_id=f"{9:064x}",
        ledger_seq=9,
        row_ts=datetime(2026, 11, 13, 0, 0, tzinfo=UTC),
    )
    assert build_daily_receipt(_request([*rows, other], rows), CONFIG).exact is True


def test_daily_receipt_verb_comparison_is_carried_in_the_receipt_and_its_row() -> None:
    rows = [_row(n) for n in range(1, 4)]
    verbs = {
        "query": 0,
        "holds": 0,
        "inbox": 2,
        "catchup": None,
        "open-rulings": 0,
        "open-claims": 0,
    }
    receipt = build_daily_receipt(_request(rows, rows, verb_diff=verbs), CONFIG)
    assert receipt.verb_diff == verbs
    row = daily_receipt_status_row(receipt, CONFIG, AFTER_CLOSE)
    assert (
        "| verb_diff=query:0,holds:0,inbox:2,catchup:err,open-rulings:0,open-claims:0 |"
        in row
    )
    unrun = build_daily_receipt(_request(rows, rows), CONFIG)
    assert "| verb_diff=not-run |" in daily_receipt_status_row(
        unrun, CONFIG, AFTER_CLOSE
    )


def test_daily_receipt_verb_comparison_runs_at_a_fixed_time_with_a_10_minute_close() -> (
    None
):
    assert (CONFIG.cutoff_utc, CONFIG.close_minutes) == ("00:00", 10)
    assert datetime(2026, 11, 13, 0, 0, tzinfo=UTC) == CUTOFF
    assert datetime(2026, 11, 13, 0, 10, tzinfo=UTC) == CLOSES
    rows = [_row(n) for n in range(1, 4)]
    before = build_daily_receipt(
        _request(rows, rows, now=CLOSES - timedelta(seconds=1)), CONFIG
    )
    assert before.state is EnumDailyReceiptState.NOT_CLOSED
    at_close = build_daily_receipt(_request(rows, rows, now=CLOSES), CONFIG)
    assert at_close.state is EnumDailyReceiptState.GREEN
    assert "| closes_at=2026-11-13T00:10:00Z |" in daily_receipt_status_row(
        at_close, CONFIG, CLOSES
    )
