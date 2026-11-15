# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port of the ledger of record after the flip: rows with a gapless ledger_seq, deduplicated by request (OMN-23154)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from omnimarket.models.work_ledger_append import ModelBusAppend


class ProtocolBusLedgerStore(Protocol):
    """What the serve appends to once it has stopped appending to the file."""

    def lookup(self, request_id: UUID) -> ModelBusAppend | None:
        """The rows this request already landed, or None."""
        ...

    def append(self, request_id: UUID, row_count: int, rows: str) -> ModelBusAppend:
        """Land ``rows`` (``row_count`` rows) once per request id and give each row the next ledger_seq.

        A second call with the same request id returns the first call's record and lands nothing."""
        ...

    def text(self) -> str:
        """Every row the store holds, in sequence order, as ledger text."""
        ...


__all__: list[str] = ["ProtocolBusLedgerStore"]
