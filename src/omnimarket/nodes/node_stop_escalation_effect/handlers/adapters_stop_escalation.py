# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory adapters of the stop escalation ports; the CI backend of the rows and the alarm channel."""

from __future__ import annotations

import threading
from datetime import datetime

from omnimarket.models.model_stop_escalation import (
    ModelAlarmReceipt,
    ModelStopEscalationRow,
)


class InMemoryStopEscalationRows:
    """Rows held in memory, one per row id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, ModelStopEscalationRow] = {}

    def append_if_absent(self, row: ModelStopEscalationRow) -> bool:
        with self._lock:
            if row.row_id in self._rows:
                return False
            self._rows[row.row_id] = row
            return True

    def read(self, row_id: str) -> ModelStopEscalationRow | None:
        with self._lock:
            return self._rows.get(row_id)

    def attach_receipt(self, row_id: str, receipt: ModelAlarmReceipt) -> None:
        with self._lock:
            self._rows[row_id] = self._rows[row_id].model_copy(
                update={"receipt": receipt}
            )

    def all_rows(self) -> list[ModelStopEscalationRow]:
        with self._lock:
            return list(self._rows.values())


class InMemoryStopAlarm:
    """An alarm channel that records what it was asked and returns a receipt per row id."""

    def __init__(self, channel: str, clock: datetime) -> None:
        self._channel = channel
        self._clock = clock
        self._lock = threading.Lock()
        self._receipts: dict[str, ModelAlarmReceipt] = {}
        self.deliveries = 0

    def deliver(self, row: ModelStopEscalationRow) -> ModelAlarmReceipt:
        with self._lock:
            known = self._receipts.get(row.row_id)
            if known is not None:
                return known
            self.deliveries += 1
            receipt = ModelAlarmReceipt(
                delivery_id=f"{self._channel}-{self.deliveries}",
                channel=self._channel,
                delivered_at=self._clock,
            )
            self._receipts[row.row_id] = receipt
            return receipt
