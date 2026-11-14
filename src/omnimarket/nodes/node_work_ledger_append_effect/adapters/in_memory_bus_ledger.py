# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-process bus ledger store: the default backend of the post-flip append port (OMN-23154).

A durable backend binds the same port; this one holds the rows of one process, gapless from ``head + 1``.
"""

from __future__ import annotations

import threading
from uuid import UUID

from omnimarket.models.work_ledger_append import ModelBusAppend


class InMemoryBusLedgerStore:
    def __init__(self, head: int = 0) -> None:
        if head < 0:
            raise ValueError("head must not be negative")
        self._lock = threading.Lock()
        self._head = head
        self._by_request: dict[UUID, ModelBusAppend] = {}
        self._ordered: list[ModelBusAppend] = []

    @property
    def head(self) -> int:
        with self._lock:
            return self._head

    def lookup(self, request_id: UUID) -> ModelBusAppend | None:
        with self._lock:
            return self._by_request.get(request_id)

    def append(self, request_id: UUID, row_count: int, rows: str) -> ModelBusAppend:
        if row_count < 1:
            raise ValueError("an append lands at least one row")
        with self._lock:
            held = self._by_request.get(request_id)
            if held is not None:
                return held
            seqs = tuple(range(self._head + 1, self._head + 1 + row_count))
            landed = ModelBusAppend(request_id=request_id, rows=rows, ledger_seqs=seqs)
            self._head = seqs[-1]
            self._by_request[request_id] = landed
            self._ordered.append(landed)
            return landed

    def text(self) -> str:
        with self._lock:
            return "".join(
                entry.rows if entry.rows.endswith("\n") else entry.rows + "\n"
                for entry in self._ordered
            )

    def rows_by_seq(self) -> tuple[tuple[int, str], ...]:
        """Each row's first line with its sequence, for a projection to fold in."""
        with self._lock:
            pairs: list[tuple[int, str]] = []
            for entry in self._ordered:
                starts = [
                    line
                    for line in entry.rows.splitlines()
                    if line[:1].isdigit() and " | " in line
                ]
                pairs.extend(zip(entry.ledger_seqs, starts, strict=True))
            return tuple(pairs)


__all__: list[str] = ["InMemoryBusLedgerStore"]
