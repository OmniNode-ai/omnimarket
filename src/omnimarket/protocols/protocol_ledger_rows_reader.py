# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of a handler that reads rows of the rolling work ledger from the bus-fed projection (OMN-20547).

The production adapter runs ``node_work_ledger_reader_effect`` under the ledger interpreter; no
implementation of this port opens a ledger file.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

__all__ = ["LedgerReadError", "ProtocolLedgerRowsReader"]


class LedgerReadError(RuntimeError):
    """The rows could not be read: an unset interpreter, a reader not named as moved, a timeout or an unreadable node.

    An unread ledger is never an empty one, so a caller treats this as "unknown", not "no rows".
    """


@runtime_checkable
class ProtocolLedgerRowsReader(Protocol):
    """One named reader's ledger rows of a window, one string per row."""

    def rows(
        self,
        *,
        reader: str,
        window_hours: int,
        kinds: Sequence[str] = (),
        lanes: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """The window's rows of the given row types (every type when ``kinds`` is empty) and lanes (every lane when ``lanes`` is empty); raises LedgerReadError."""
