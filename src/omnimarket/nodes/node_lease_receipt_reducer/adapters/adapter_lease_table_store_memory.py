# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory default store of the lease receipt reducer.

One dict and one lock: the compare and the write happen under the same lock, so
two threads that read one version cannot both write. A real-backend store (the
same protocol over a durable table) is a later overlay and is not part of this
slice: this store forgets its tables when the process ends.
"""

from __future__ import annotations

import threading

from omnimarket.models.lease_receipt.model_lease_table import ModelLeaseTable


class InMemoryLeaseTableStore:
    """The held tables of the reducer, in process memory."""

    def __init__(self) -> None:
        self._tables: dict[str, ModelLeaseTable] = {}
        self._lock = threading.Lock()

    def read(self, kind: str) -> ModelLeaseTable | None:
        with self._lock:
            return self._tables.get(kind)

    def compare_and_write(
        self, kind: str, expected_version: int | None, table: ModelLeaseTable
    ) -> bool:
        with self._lock:
            held = self._tables.get(kind)
            held_version = None if held is None else held.version
            if held_version != expected_version:
                return False
            self._tables[kind] = table
            return True


__all__: list[str] = ["InMemoryLeaseTableStore"]
