# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the ledger rows the operator capture needs: rulings, captures and closures.

Only rows that can matter are kept: RULING rows, ``operator-capture`` rows and rows citing
``closes-ask=``. The one-shot commands read the ledger reader node's 720 h window
(:func:`bus_relevant_rows`); an unread window raises ``LedgerReadError``, never an empty answer. The
ledger host's own serve process names its ledger file and reads that file and its roll archives
(``archive/*.md`` beside it) instead (:func:`relevant_rows`); a missing archive directory is not an
error there, a missing ledger is.
"""

from __future__ import annotations

import os
from pathlib import Path

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    SubprocessLedgerRowsReader,
)
from omnimarket.protocols.protocol_ledger_rows_reader import ProtocolLedgerRowsReader

NEEDLES = ("| RULING |", "lane=operator-capture", "closes-ask=")
#: The overlay's moved reader that answers the capture's window, and the reader node's 30 day bound.
READER = "operator-capture"
WINDOW_HOURS = 720


def bus_relevant_rows(
    reader: ProtocolLedgerRowsReader | None = None,
) -> tuple[str, ...]:
    """The reader node's window, narrowed to the rows the capture reads; no reader: the subprocess reader."""
    lines = (reader or SubprocessLedgerRowsReader(os.environ)).rows(
        reader=READER, window_hours=WINDOW_HOURS
    )
    return tuple(line for line in lines if any(n in line for n in NEEDLES))


def relevant_rows(ledger_path: Path, *, archives: bool = True) -> tuple[str, ...]:
    paths: list[Path] = []
    if archives:
        archive_dir = ledger_path.parent / "archive"
        if archive_dir.is_dir():
            paths.extend(sorted(archive_dir.glob("*.md")))
    paths.append(ledger_path)
    rows: list[str] = []
    for path in paths:
        with path.open(encoding="utf-8", errors="replace") as handle:
            rows.extend(
                line.rstrip("\n") for line in handle if any(n in line for n in NEEDLES)
            )
    return tuple(rows)


__all__ = [
    "NEEDLES",
    "READER",
    "WINDOW_HOURS",
    "bus_relevant_rows",
    "relevant_rows",
]
