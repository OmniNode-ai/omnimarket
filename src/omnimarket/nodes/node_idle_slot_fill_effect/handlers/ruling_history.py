# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ruling history the throughput tick reads: the ledger reader node's window, never a ledger file."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.adapter_work_ledger_rows_reader import (
    SubprocessLedgerRowsReader,
)
from omnimarket.protocols.protocol_ledger_rows_reader import (
    LedgerReadError,
    ProtocolLedgerRowsReader,
)

from .ruling_recovery import parse_rows

#: The overlay's moved reader that answers the ruling history; the node's 30 day window, as the fixer-hold read.
RULING_HISTORY_READER = "idle-fill-ruling-recovery"
RULING_HISTORY_WINDOW_HOURS = 720


def read_ruling_rows(
    environ: Mapping[str, str], reader: ProtocolLedgerRowsReader | None = None
) -> list[dict[str, str]]:
    """The ruling history of the reader node's 720 h window; an unread history raises ValueError, never no rows."""
    try:
        lines = (reader or SubprocessLedgerRowsReader(environ)).rows(
            reader=RULING_HISTORY_READER, window_hours=RULING_HISTORY_WINDOW_HOURS
        )
    except LedgerReadError as exc:
        raise ValueError(str(exc)) from exc
    return parse_rows("\n".join(lines))
