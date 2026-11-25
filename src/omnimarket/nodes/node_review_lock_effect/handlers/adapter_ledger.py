# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger adapters of the review lock: a file reader and a command writer.

The reader reads the live ledger file and the rolls beside it (``archive/*.md`` named for a date in
the last three days), oldest roll first,
keeping only STATUS rows that name the repository. The writer appends one row by running the ledger
writer command (``onex-ledger <ledger> --append <row>``), the only way a row reaches the ledger; a
non-zero exit or a missing command raises OSError.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from omnimarket.nodes.node_review_lock_effect.handlers.ledger_rows import parse_row
from omnimarket.nodes.node_review_lock_effect.models.model_review_lock import (
    ModelStatusRow,
)

APPEND_TIMEOUT_SECONDS = 120
ARCHIVE_DAYS = 3
_ROLL_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _utc_now() -> datetime:
    return datetime.now(UTC)


class FileLedgerStatusReader:
    """STATUS rows of one repository from the ledger file and its archive rolls."""

    def __init__(
        self,
        ledger_path: Path,
        archive_days: int = ARCHIVE_DAYS,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._ledger = ledger_path
        self._archive_days = archive_days
        self._clock = clock

    def _recent_rolls(self) -> list[Path]:
        """Archive rolls named for a date within the window; a head older than that is stale."""
        floor = (self._clock() - timedelta(days=self._archive_days)).date()
        rolls = []
        for path in sorted((self._ledger.parent / "archive").glob("*.md")):
            match = _ROLL_DATE.search(path.name)
            if match is not None and date.fromisoformat(match.group(1)) >= floor:
                rolls.append(path)
        return rolls

    def status_rows(self, repo: str) -> list[ModelStatusRow]:
        if not self._ledger.is_file():
            raise FileNotFoundError(f"ledger file not found: {self._ledger}")
        files = [*self._recent_rolls(), self._ledger]
        rows: list[ModelStatusRow] = []
        for path in files:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if " | STATUS | " not in line or repo not in line:
                        continue
                    row = parse_row(line)
                    if row is not None and row.row_type == "STATUS":
                        rows.append(row)
        return rows


class CommandLedgerRowWriter:
    """Appends a row by running ``<command...> <ledger> --append <row>``."""

    def __init__(self, command: Sequence[str], ledger_path: Path) -> None:
        self._command = list(command)
        self._ledger = ledger_path

    def append(self, row: str) -> None:
        argv = [*self._command, str(self._ledger), "--append", row]
        try:
            done = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=APPEND_TIMEOUT_SECONDS,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            raise OSError(f"ledger writer did not run: {exc}") from exc
        if done.returncode != 0:
            tail = (done.stderr or done.stdout).strip().splitlines()[-1:] or [""]
            raise OSError(f"ledger writer exit {done.returncode}: {tail[0]}")
