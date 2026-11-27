# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The markdown ledger's files in file order: archive splits by date, then the live file (OMN-21947).

Reads text only. A row is what ``split_ledger_rows`` calls a row, so the row identity is the
one the parity check and the projection already share. Line numbers are ``str.splitlines()``
numbers, the numbers a ``<ledger>:<line>`` citation uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.work_ledger_text import (
    roll_headers,
    split_ledger_rows_with_lines,
)

LIVE_KEY = "live"
SPLIT_GLOB = "ROLLING_WORK_LEDGER_*-split.md"


@dataclass(frozen=True)
class LedgerFileRow:
    file_key: str
    first_line: int
    last_line: int
    raw_row: str

    @property
    def stamp(self) -> datetime:
        return datetime.strptime(self.raw_row[:20], "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=UTC
        )


@dataclass(frozen=True)
class LedgerRollBlock:
    """One roll: ``entries`` rows moved from the head of the live file into a split."""

    file_key: str
    first_row_line: int
    rolled_at: datetime
    entries: int


@dataclass(frozen=True)
class LedgerHistory:
    """Every row in file order, the roll blocks in order, and the live file's own header."""

    live_name: str
    rows: tuple[LedgerFileRow, ...]
    blocks: tuple[LedgerRollBlock, ...]
    live_preamble_lines: int


def read_ledger_history(ledger: Path, archive_dir: Path | None) -> LedgerHistory:
    """Archive splits sorted by name (the roll date), then the live file."""
    paths: list[tuple[str, Path]] = []
    if archive_dir is not None:
        paths.extend((p.name, p) for p in sorted(archive_dir.glob(SPLIT_GLOB)))
    paths.append((LIVE_KEY, ledger))
    rows: list[LedgerFileRow] = []
    blocks: list[LedgerRollBlock] = []
    preamble = 0
    for key, path in paths:
        text = path.read_text(encoding="utf-8")
        found = split_ledger_rows_with_lines(text)
        rows.extend(LedgerFileRow(key, first, last, raw) for first, last, raw in found)
        if key == LIVE_KEY:
            preamble = (found[0][0] - 1) if found else len(text.splitlines())
            continue
        blocks.extend(
            LedgerRollBlock(key, line, at, count)
            for line, at, count in roll_headers(text)
        )
    return LedgerHistory(
        live_name=ledger.name,
        rows=tuple(rows),
        blocks=tuple(blocks),
        live_preamble_lines=preamble,
    )


__all__ = [
    "LIVE_KEY",
    "LedgerFileRow",
    "LedgerHistory",
    "LedgerRollBlock",
    "read_ledger_history",
]
