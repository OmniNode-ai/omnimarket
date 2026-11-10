# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapters of join J6's ports; the CI backend of the audit sink and the projection."""

from __future__ import annotations

import threading

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_gate_store_join_j6 import ModelJoinJ6AuditRow
from omnimarket.protocols.protocol_gate_store import ProtocolGateStore


class InMemoryJoinJ6AuditSink:
    """Durable in the sense of the join: a row once appended is read back by its id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, ModelJoinJ6AuditRow] = {}

    def append_if_absent(self, row: ModelJoinJ6AuditRow) -> bool:
        with self._lock:
            if row.row_id in self._rows:
                return False
            self._rows[row.row_id] = row
            return True

    def read(self, row_id: str) -> ModelJoinJ6AuditRow | None:
        with self._lock:
            return self._rows.get(row_id)

    def rows_for(self, piece: str) -> list[ModelJoinJ6AuditRow]:
        """Every row of one piece, oldest first."""
        with self._lock:
            return [r for r in self._rows.values() if r.piece == piece]


class InMemoryJoinJ6Projection:
    """The projection of the audit rows, held in memory."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, ModelJoinJ6AuditRow] = {}

    def project(self, row: ModelJoinJ6AuditRow) -> None:
        with self._lock:
            self._rows[row.row_id] = row

    def read(self, row_id: str) -> ModelJoinJ6AuditRow | None:
        with self._lock:
            return self._rows.get(row_id)

    def rows_for(self, piece: str) -> list[ModelJoinJ6AuditRow]:
        """Every projected row of one piece."""
        with self._lock:
            return [r for r in self._rows.values() if r.piece == piece]


class DroppingJoinJ6AuditSink(InMemoryJoinJ6AuditSink):
    """Injected break ``drop-audit-row``: the sink reports the append and keeps nothing (the join's failing control)."""

    def append_if_absent(self, row: ModelJoinJ6AuditRow) -> bool:
        return True


class UnansweringStopGateStore:
    """The fence drill's injected fault: a stop's commit gets no answer for ``hold_seconds``, then the call raises.

    Every other operation is the wrapped store's own, so a send of another piece is decided by the real store.
    """

    def __init__(self, inner: ProtocolGateStore, hold_seconds: float) -> None:
        self._inner = inner
        self._hold = hold_seconds
        self._release = threading.Event()

    def transact(self, request: ModelGateStoreRequest) -> ModelGateStoreResult:
        if request.operation is EnumGateStoreOperation.RECORD_SAFETY:
            self._release.wait(timeout=self._hold)
            raise TimeoutError("released after the join gave up")
        return self._inner.transact(request)

    def release(self) -> None:
        """Let the held commit return now (a run's cleanup)."""
        self._release.set()
