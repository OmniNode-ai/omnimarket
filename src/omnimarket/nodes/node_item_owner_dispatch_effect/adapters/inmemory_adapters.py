# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend: a ledger list that is both read and appended to, fixed plan rules and an atomic reservation."""

from __future__ import annotations

import threading

from omnimarket.nodes.node_item_owner_dispatch_effect.protocols import (
    LedgerUnreadableError,
    PlanOwnerUnreadableError,
    RowAppendFailedError,
)


class InMemoryLedger:
    """The rows read and appended, in order, under one lock (the ledger host appends one row at a time)."""

    def __init__(self, rows: tuple[str, ...] = ()) -> None:
        self.rows: list[str] = list(rows)
        self.unreadable: str | None = None
        self.refuse_append: str | None = None
        self._lock = threading.Lock()

    def read(self) -> tuple[str, ...]:
        if self.unreadable is not None:
            raise LedgerUnreadableError(self.unreadable)
        with self._lock:
            return tuple(self.rows)

    def append(self, row: str) -> None:
        if self.refuse_append is not None:
            raise RowAppendFailedError(self.refuse_append)
        with self._lock:
            self.rows.append(row)


class InMemoryPlanOwner:
    def __init__(self, missing: tuple[str, ...] = ()) -> None:
        self.missing = missing
        self.unreadable: str | None = None

    def read(self) -> tuple[str, ...]:
        if self.unreadable is not None:
            raise PlanOwnerUnreadableError(self.unreadable)
        return self.missing


class InMemoryReservation:
    """First caller of a key wins, atomically, as a Temporal workflow id start does."""

    def __init__(self) -> None:
        self.keys: set[str] = set()
        self._lock = threading.Lock()

    def reserve(self, key: str) -> bool:
        with self._lock:
            if key in self.keys:
                return False
            self.keys.add(key)
            return True


__all__ = ["InMemoryLedger", "InMemoryPlanOwner", "InMemoryReservation"]
