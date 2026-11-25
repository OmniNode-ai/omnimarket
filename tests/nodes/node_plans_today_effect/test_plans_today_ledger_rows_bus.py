# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""plans-today reads the work ledger's rows through the rows loader node, not through a ledger file."""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from omnimarket.handlers.ledger_rows_via_node import ROWS_LOADER_MODULE
from omnimarket.nodes.node_plans_today_effect.handlers import (
    handler_plans_today as plans_today,
)

ROWS = (
    "2026-09-29T10:00:00Z | CLAIM | lane=lane-a | ticket=OMN-2001 | actor=x | scope text",
    "2026-09-29T12:30:00Z | TERMINAL | lane=lane-a | ticket=OMN-2001 | outcome=done",
)


class _Answer:
    """Stands in for the module's child-process seam and records each argv."""

    def __init__(self, code: int, rows: tuple[str, ...] = (), err: str = "") -> None:
        self.code = code
        self.rows = rows
        self.err = err
        self.calls: list[list[str]] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv, self.code, "".join(f"{r}\n" for r in self.rows), self.err
        )


def test_rows_come_from_the_rows_loader_with_no_plugin_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Answer(0, ROWS)
    monkeypatch.setattr(plans_today, "run", fake)
    rows, newest = plans_today.read_ledger(14, None)
    assert [r.kind for r in rows] == ["CLAIM", "TERMINAL"]
    assert rows[0].cell("lane") == "lane-a"
    assert newest == "2026-09-29T12:30:00Z"
    (argv,) = fake.calls
    assert argv[1:3] == ["-m", ROWS_LOADER_MODULE]
    assert argv[-4:] == ["--reader", "plans-today", "--window-hours", "336"]


def test_an_unanswered_read_refuses_and_asks_the_loader_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Answer(69, err="READER unreadable")
    monkeypatch.setattr(plans_today, "run", fake)
    with pytest.raises(plans_today.RefusedError, match="exited 69"):
        plans_today.read_ledger(14, None)
    assert len(fake.calls) == 1
