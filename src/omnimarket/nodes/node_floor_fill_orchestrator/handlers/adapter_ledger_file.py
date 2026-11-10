# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``ProtocolWorkLedgerStore`` over the rolling ledger file: the rows that carry one lane's name."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

_CELL_SEPARATOR = " | "


class FileWorkLedgerStore:
    """Reads the ledger file at each call. An unreadable file raises ``OSError``; the caller wraps it."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, path: str) -> None:
        self._path = Path(path)

    def rows_for_lane(self, lane: str) -> tuple[str, ...]:
        """The rows, in file order, with a cell exactly equal to ``lane=<lane>``."""
        wanted = f"lane={lane}"
        rows: list[str] = []
        with self._path.open(encoding="utf-8") as handle:
            for line in handle:
                row = line.rstrip("\n")
                if wanted in (cell.strip() for cell in row.split(_CELL_SEPARATOR)):
                    rows.append(row)
        return tuple(rows)

    def queued_rows(self, lane: str, run_id: str) -> tuple[str, ...]:
        """The fill never queues a row."""
        return ()


__all__: list[str] = ["FileWorkLedgerStore"]
