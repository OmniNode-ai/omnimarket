# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The history-range read of the rows loader: its argv and what an unanswered read does."""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError
from omnimarket.handlers.ledger_rows_via_node import (
    ROWS_LOADER_MODULE,
    read_ledger_rows_range_via_node,
)

SINCE = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 9, 30, 12, 30, 15, tzinfo=UTC)


class _Runner:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.code, self.out, self.err = code, out, err
        self.argv: list[str] = []

    def __call__(
        self, argv: list[str], timeout_s: float
    ) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        return subprocess.CompletedProcess(argv, self.code, self.out, self.err)


def test_the_range_argv_names_since_until_and_kinds_and_no_window() -> None:
    runner = _Runner(0, "a\nb\n")
    rows = read_ledger_rows_range_via_node(
        "r", SINCE, UNTIL, kinds=("HOLD", "RELEASE"), runner=runner
    )
    assert rows == ("a", "b")
    assert runner.argv == [
        sys.executable,
        "-m",
        ROWS_LOADER_MODULE,
        "--reader",
        "r",
        "--since",
        "2026-09-01T00:00:00Z",
        "--until",
        "2026-09-30T12:30:15Z",
        "--kind",
        "HOLD",
        "--kind",
        "RELEASE",
    ]


def test_an_open_range_has_no_until() -> None:
    runner = _Runner(0)
    read_ledger_rows_range_via_node("r", SINCE, runner=runner)
    assert "--until" not in runner.argv
    assert "--window-hours" not in runner.argv


@pytest.mark.parametrize("code", [1, 10, 69])
def test_a_refused_range_is_unreadable_never_empty(code: int) -> None:
    with pytest.raises(LedgerRowsUnreadableError, match=f"exited {code}"):
        read_ledger_rows_range_via_node("r", SINCE, runner=_Runner(code, "", "no"))
