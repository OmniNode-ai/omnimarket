# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A stand-in for the bus the silent failure alarm reads and appends its ledger rows over.

The alarm reads its window of rows through the rows loader node and appends each row through the append
node. A test gives it this file-backed fake instead: the rows it reads are the lines of one fixture file
and an append adds one line to it, so a test can seed rows before a tick and read what the tick wrote.
A fixture file that does not exist is an unreadable window, the same as the loader answering that it
could not read.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.ledger_rows_unreadable_error import LedgerRowsUnreadableError

LEDGER_FIXTURE_NAME = "ledger.md"


class FileBackedAlarmLedger:
    def __init__(self, path: Path, *, append_ok: bool = True) -> None:
        self.path = path
        self.append_ok = append_ok
        self.appended: list[str] = []
        self.windows: list[int] = []

    def read_rows(self, window_hours: int) -> str:
        self.windows.append(window_hours)
        if not self.path.is_file():
            raise LedgerRowsUnreadableError("the rows loader answered no rows")
        return self.path.read_text(encoding="utf-8")

    def append(self, row: str) -> tuple[bool, str]:
        if not self.append_ok:
            return False, "append refused"
        self.appended.append(row)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(row + "\n")
        return True, "appended"


def install_alarm_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the alarm's default ledger the fixture file ``<registry_root>/ledger.md`` of its options."""
    from omnimarket.nodes.node_silent_failure_alarm_effect.handlers.handler_silent_failure_alarm import (
        HandlerSilentFailureAlarm,
    )
    from omnimarket.nodes.node_silent_failure_alarm_effect.models import (
        ModelSilentFailureAlarmOptions,
    )

    original = HandlerSilentFailureAlarm.__init__

    def init(
        self: HandlerSilentFailureAlarm, options: Any = None, **kwargs: Any
    ) -> None:
        options = options or ModelSilentFailureAlarmOptions.from_environment()
        if kwargs.get("ledger") is None:
            kwargs["ledger"] = FileBackedAlarmLedger(
                options.registry_root / LEDGER_FIXTURE_NAME
            )
        original(self, options, **kwargs)

    monkeypatch.setattr(HandlerSilentFailureAlarm, "__init__", init)
