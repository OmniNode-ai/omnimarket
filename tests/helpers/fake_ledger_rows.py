# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ProtocolLedgerRowsReader that answers from rows the test hands it and opens no file (OMN-20547)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from omnimarket.handlers.handler_ledger_claims import claim_lane
from omnimarket.protocols.protocol_ledger_rows_reader import LedgerReadError


def quiet_heartbeat_row() -> str:
    """One quiet lane's STATUS row stamped now: a live ledger always has a row in its last hour, and it opens no claim."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{stamp} | STATUS | lane=ledger-heartbeat | principal=p | alive"


class FakeLedgerRows:
    """Rows by type; every call is kept so a test asserts which reader named which window."""

    def __init__(self, lines: Sequence[str] = (), error: str = "") -> None:
        self.lines = list(lines)
        self.error = error
        self.calls: list[tuple[str, int, tuple[str, ...]]] = []
        #: The lanes each call named, parallel to ``calls`` (empty tuple: every lane).
        self.lanes_asked: list[tuple[str, ...]] = []

    def rows(
        self,
        *,
        reader: str,
        window_hours: int,
        kinds: Sequence[str] = (),
        lanes: Sequence[str] = (),
    ) -> tuple[str, ...]:
        self.calls.append((reader, window_hours, tuple(kinds)))
        self.lanes_asked.append(tuple(lanes))
        if self.error:
            raise LedgerReadError(self.error)
        # A line with no " | " cell is a continuation of a multi-line row: the reader passes it through.
        return tuple(
            line
            for line in self.lines
            if (not kinds or " | " not in line or line.split(" | ")[1] in kinds)
            and (not lanes or " | " not in line or claim_lane(line) in lanes)
        )
