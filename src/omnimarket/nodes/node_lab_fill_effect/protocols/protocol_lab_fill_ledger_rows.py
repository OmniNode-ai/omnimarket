# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port the lab-fill select step reads its rolling ledger through (OMN-19998).

The select step's ledger arrives only from the bus-fed work ledger query node, never from a file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LabFillLedgerRows:
    """One window read of the rolling ledger: its status, rows and why it is not ok."""

    status: str
    lines: tuple[str, ...] = ()
    reason: str = ""
    watermark_age_s: int | None = None


class ProtocolLabFillLedgerRows(Protocol):
    """Reads the rolling ledger's rows of a window from the bus-fed work ledger query node, never from the file."""

    def read_rows(self, reader: str, window_hours: int) -> LabFillLedgerRows: ...
