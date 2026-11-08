# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory conditional store of the fill's rows (the default profile).

It implements ``ProtocolFloorStateStore``: a write lands only when the stored version is the one the writer
read (version 0 is "no row", so the first write is an insert-if-absent). The table-backed store over
``floor_fill_state`` (omnibase_infra migration 113) is in ``store_postgres.py``; the crash, race and process tests also run against the SQLite store in ``tests/support_sqlite_store.py``.
"""

from __future__ import annotations

import threading

from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelStateRead,
)


class InMemoryFloorStateStore:
    """Versioned rows in process; one lock makes the compare and the write one step."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()

    def load(self, key: str) -> ModelStateRead:
        with self._lock:
            stored = self._rows.get(key)
        if stored is None:
            return ModelStateRead(payload=None, version=0)
        return ModelStateRead(payload=stored[0], version=stored[1])

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        with self._lock:
            stored = self._rows.get(key)
            current = stored[1] if stored is not None else 0
            if current != expected_version:
                return False
            self._rows[key] = (payload, new_version)
            return True


__all__: list[str] = ["InMemoryFloorStateStore"]
