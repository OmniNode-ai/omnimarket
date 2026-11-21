# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger rows reader adapter runs the reader module under the ledger interpreter and opens no file (OMN-20547)."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    READER_MODULE,
    SubprocessLedgerRowsReader,
)
from omnimarket.protocols.protocol_ledger_rows_reader import LedgerReadError

ENV = {
    "ONEX_LEDGER_BUS_APPEND_COMMAND": "/venv/bin/python -m omnimarket.work_ledger_bus.cli append --bus-lane dev",
    "ONEX_LEDGER_PATH": "/does/not/exist/ROLLING_WORK_LEDGER.md",
}


class Runner:
    def __init__(self, code: int, out: str = "", err: str = "") -> None:
        self.argv: list[str] = []
        self.done = subprocess.CompletedProcess([], code, stdout=out, stderr=err)

    def __call__(self, argv: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        return self.done


def test_the_reader_module_runs_under_the_first_word_of_the_append_command() -> None:
    run = Runner(0, "a | HOLD | x\n\nb | RELEASE | y\n")
    rows = SubprocessLedgerRowsReader(ENV, run=run).rows(
        reader="idle-fill-holds", window_hours=720, kinds=("HOLD", "RELEASE")
    )
    assert rows == ("a | HOLD | x", "b | RELEASE | y")
    assert run.argv == [
        "/venv/bin/python",
        "-m",
        READER_MODULE,
        "--reader",
        "idle-fill-holds",
        "--query",
        "rows",
        "--window-hours",
        "720",
        "--kind",
        "HOLD",
        "--kind",
        "RELEASE",
    ]


def test_a_stale_answer_is_returned_and_its_reader_line_is_kept(
    capsys: pytest.CaptureFixture[str],
) -> None:
    run = Runner(1, "a | HOLD | x\n", "READER reader=r status=stale\n")
    rows = SubprocessLedgerRowsReader(ENV, run=run).rows(reader="r", window_hours=1)
    assert rows == ("a | HOLD | x",)
    assert "status=stale" in capsys.readouterr().err


@pytest.mark.parametrize("code", [3, 69, 2])
def test_not_moved_unreadable_and_other_exits_raise_never_an_empty_read(
    code: int,
) -> None:
    run = Runner(code, "", "READER reader=r status=not-moved\n")
    with pytest.raises(LedgerReadError, match=f"exited {code}"):
        SubprocessLedgerRowsReader(ENV, run=run).rows(reader="r", window_hours=1)


@pytest.mark.parametrize("command", ["", "   ", "'unbalanced"])
def test_an_unset_or_unsplittable_interpreter_raises(command: str) -> None:
    env = {"ONEX_LEDGER_BUS_APPEND_COMMAND": command}
    with pytest.raises(LedgerReadError, match="ONEX_LEDGER_BUS_APPEND_COMMAND"):
        SubprocessLedgerRowsReader(env, run=Runner(0)).rows(reader="r", window_hours=1)


def test_a_timeout_and_a_missing_binary_raise() -> None:
    def timed_out(argv: list[str], **_kw: Any) -> None:
        raise subprocess.TimeoutExpired(argv, 1)

    def missing(argv: list[str], **_kw: Any) -> None:
        raise FileNotFoundError(argv[0])

    with pytest.raises(LedgerReadError, match="timed out"):
        SubprocessLedgerRowsReader(ENV, timeout_seconds=1, run=timed_out).rows(
            reader="r", window_hours=1
        )
    with pytest.raises(LedgerReadError, match="could not run"):
        SubprocessLedgerRowsReader(ENV, run=missing).rows(reader="r", window_hours=1)
