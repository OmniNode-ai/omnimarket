# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The parity receipt's exact_kinds cell reaches the reader, and kinds_complete
judges a window's parity against the kinds a caller names."""

from __future__ import annotations

from datetime import UTC, datetime

from omnimarket.models.work_ledger_query import (
    ModelWorkLedgerParity,
    ModelWorkLedgerParityDay,
    ModelWorkLedgerRowRecord,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_replay import (
    LedgerRow,
    kinds_complete,
    parse_rows,
    window_parity,
)


def _receipt_row(row_id: str, text: str) -> ModelWorkLedgerRowRecord:
    return ModelWorkLedgerRowRecord(
        row_id=row_id,
        row_ts=datetime(2026, 11, 15, 8, 44, 58, tzinfo=UTC),
        row_type="STATUS",
        row_lane="work-ledger-parity",
        text=text,
        raw_row=text,
        source="test",
    )


REAL_ROW = (
    "2026-11-15T08:44:58Z | STATUS | lane=work-ledger-parity | day=2026-11-14 "
    "| exact=no | exact_kinds=ACK:yes,CLAIM:yes,RELEASE:yes,STATUS:no,TERMINAL:no "
    "| missing=2 | extra=0"
)


def _rows(*texts: str) -> list[LedgerRow]:
    records = [_receipt_row(f"r{i}", t) for i, t in enumerate(texts)]
    return parse_rows(records)


def test_window_parity_parses_exact_kinds() -> None:
    rows = _rows(REAL_ROW)
    parity = window_parity(["2026-11-14"], rows)
    day = parity.days[0]
    assert day.exact is False
    assert day.exact_kinds == {
        "ACK": True,
        "CLAIM": True,
        "RELEASE": True,
        "STATUS": False,
        "TERMINAL": False,
    }


def test_kinds_complete_all_yes_with_trailing_unmeasured() -> None:
    # Two days: 2026-11-13 exact with all kinds yes, 2026-11-14 unmeasured.
    day1 = ModelWorkLedgerParityDay(
        day="2026-11-13",
        exact=True,
        exact_kinds={"CLAIM": True, "RELEASE": True, "TERMINAL": True},
    )
    day2 = ModelWorkLedgerParityDay(day="2026-11-14", exact=None)
    parity = ModelWorkLedgerParity(
        status=ModelWorkLedgerParityDay.__mro__
        and __import__(
            "omnimarket.models.work_ledger_query",
            fromlist=["EnumWorkLedgerParityStatus"],
        ).EnumWorkLedgerParityStatus.UNMEASURED,
        days=(day1, day2),
    )
    assert (
        kinds_complete(
            parity, ["CLAIM", "RELEASE", "TERMINAL"], unmeasured_days_allowed=1
        )
        == ""
    )


def test_kinds_complete_incomplete_terminal() -> None:
    rows = _rows(REAL_ROW)
    parity = window_parity(["2026-11-14"], rows)
    assert (
        kinds_complete(
            parity, ["CLAIM", "RELEASE", "TERMINAL"], unmeasured_days_allowed=1
        )
        == "incomplete:TERMINAL@2026-11-14"
    )


def test_kinds_complete_unmeasured_two_days() -> None:
    day1 = ModelWorkLedgerParityDay(day="2026-11-13", exact=None)
    day2 = ModelWorkLedgerParityDay(day="2026-11-14", exact=None)
    parity = ModelWorkLedgerParity(
        status=__import__(
            "omnimarket.models.work_ledger_query",
            fromlist=["EnumWorkLedgerParityStatus"],
        ).EnumWorkLedgerParityStatus.UNMEASURED,
        days=(day1, day2),
    )
    assert kinds_complete(parity, ["CLAIM"], unmeasured_days_allowed=1) == "unmeasured"


def test_kinds_complete_no_days() -> None:
    assert kinds_complete(None, ["CLAIM"], unmeasured_days_allowed=1) == "no_days"


def test_old_parity_day_without_exact_kinds_validates() -> None:
    day = ModelWorkLedgerParityDay(day="2026-11-14", exact=None)
    assert day.exact_kinds == {}


def test_kinds_complete_no_kinds_reason() -> None:
    # A measured day with an empty exact_kinds fails with no_kinds@<day>.
    day = ModelWorkLedgerParityDay(day="2026-11-14", exact=True, exact_kinds={})
    parity = ModelWorkLedgerParity(
        status=__import__(
            "omnimarket.models.work_ledger_query",
            fromlist=["EnumWorkLedgerParityStatus"],
        ).EnumWorkLedgerParityStatus.EXACT,
        days=(day,),
    )
    assert (
        kinds_complete(parity, ["CLAIM"], unmeasured_days_allowed=1)
        == "no_kinds@2026-11-14"
    )


def test_kinds_complete_control_change_input() -> None:
    # Control: make TERMINAL yes, the reason changes from incomplete to "".
    day = ModelWorkLedgerParityDay(
        day="2026-11-14",
        exact=True,
        exact_kinds={"CLAIM": True, "RELEASE": True, "TERMINAL": True},
    )
    parity = ModelWorkLedgerParity(
        status=__import__(
            "omnimarket.models.work_ledger_query",
            fromlist=["EnumWorkLedgerParityStatus"],
        ).EnumWorkLedgerParityStatus.EXACT,
        days=(day,),
    )
    assert (
        kinds_complete(
            parity, ["CLAIM", "RELEASE", "TERMINAL"], unmeasured_days_allowed=1
        )
        == ""
    )


def _day(
    day: str, *, exact: bool | None, kinds: dict[str, bool]
) -> ModelWorkLedgerParityDay:
    return ModelWorkLedgerParityDay(day=day, exact=exact, exact_kinds=kinds)


def test_unmeasured_day_before_a_measured_day_is_a_hole() -> None:
    complete = {"CLAIM": True, "RELEASE": True, "TERMINAL": True}
    hole = ModelWorkLedgerParity(
        status="unmeasured",  # type: ignore[arg-type]
        days=(
            _day("2026-11-13", exact=None, kinds={}),
            _day("2026-11-14", exact=True, kinds=complete),
        ),
    )
    trailing = ModelWorkLedgerParity(
        status="unmeasured",  # type: ignore[arg-type]
        days=(
            _day("2026-11-13", exact=True, kinds=complete),
            _day("2026-11-14", exact=None, kinds={}),
        ),
    )
    kinds = ("CLAIM", "TERMINAL", "RELEASE")
    assert kinds_complete(hole, kinds, unmeasured_days_allowed=1) == "unmeasured"
    # The control: the same single unmeasured day at the end of the window is the open day.
    assert kinds_complete(trailing, kinds, unmeasured_days_allowed=1) == ""
