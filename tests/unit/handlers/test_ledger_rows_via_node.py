# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows a reader gets from the rows loader node: its argv, its bounds and what an unanswered read does."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError
from omnimarket.handlers.ledger_rows_via_node import (
    MAX_WINDOW_HOURS,
    ROWS_LOADER_MODULE,
    read_ledger_rows_via_node,
    window_hours_covering,
)


class _Runner:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.code = code
        self.out = out
        self.err = err
        self.argv: list[str] = []
        self.timeout = 0.0

    def __call__(
        self, argv: list[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        self.timeout = timeout_s
        return subprocess.CompletedProcess(argv, self.code, self.out, self.err)


def test_the_rows_are_the_printed_lines_of_the_loader_for_the_named_reader() -> None:
    runner = _Runner(0, "row one\nrow two\n")
    rows = read_ledger_rows_via_node("some-reader", 48, runner=runner, timeout_s=9.0)
    assert rows == ("row one", "row two")
    assert runner.argv == [
        sys.executable,
        "-m",
        ROWS_LOADER_MODULE,
        "--reader",
        "some-reader",
        "--window-hours",
        "48",
    ]
    assert runner.timeout == 9.0


@pytest.mark.parametrize("code", [1, 3, 10, 69])
def test_a_loader_that_does_not_answer_is_unreadable_never_empty(code: int) -> None:
    with pytest.raises(LedgerRowsUnreadableError, match=f"exited {code}"):
        read_ledger_rows_via_node("r", 1, runner=_Runner(code, "", "boom"))


def test_a_loader_that_cannot_run_is_unreadable() -> None:
    def broken(argv: list[str], timeout_s: float) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(argv[0])

    with pytest.raises(LedgerRowsUnreadableError, match="did not run"):
        read_ledger_rows_via_node("r", 1, runner=broken)


def test_the_window_covers_the_start_with_a_margin_and_stays_inside_the_loader_bounds() -> (
    None
):
    now = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    assert window_hours_covering(now - timedelta(hours=12), now) == 14
    assert window_hours_covering(now, now) == 2
    assert window_hours_covering(now - timedelta(days=90), now) == MAX_WINDOW_HOURS
