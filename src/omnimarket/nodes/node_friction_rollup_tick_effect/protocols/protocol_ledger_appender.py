# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The appender that puts the tick's STATUS row on the ledger: a protocol, the real command and an in-memory fake."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ModelAppendOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    detail: str


class ProtocolLedgerAppender(Protocol):
    def append(self, ledger: Path, row: str) -> ModelAppendOutcome: ...


class SubprocessLedgerAppender:
    """Runs ``<command> <ledger> --append <row>`` exactly as the timer's append_row did."""

    def __init__(self, command: Sequence[str], timeout_seconds: int) -> None:
        self._command = list(command)
        self._timeout = timeout_seconds

    def append(self, ledger: Path, row: str) -> ModelAppendOutcome:
        try:
            done = subprocess.run(
                [*self._command, str(ledger), "--append", row],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ModelAppendOutcome(
                ok=False, detail=f"ledger lock did not run: {exc}"
            )
        detail = (done.stderr.strip() or done.stdout.strip())[:400]
        return ModelAppendOutcome(
            ok=done.returncode == 0, detail=f"rc={done.returncode} {detail}".strip()
        )


class InMemoryLedgerAppender:
    """Keeps appended rows in a list; ``refuse`` makes every append fail with that detail."""

    def __init__(self, refuse: str | None = None) -> None:
        self.rows: list[tuple[Path, str]] = []
        self._refuse = refuse

    def append(self, ledger: Path, row: str) -> ModelAppendOutcome:
        if self._refuse is not None:
            return ModelAppendOutcome(ok=False, detail=self._refuse)
        self.rows.append((ledger, row))
        return ModelAppendOutcome(ok=True, detail="rc=0")
