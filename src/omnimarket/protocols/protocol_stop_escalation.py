# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two ports of the stop escalation effect: the escalation rows and the alarm channel."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from omnimarket.models.model_stop_escalation import (
    ModelAlarmReceipt,
    ModelStopEscalationRow,
)


@runtime_checkable
class ProtocolStopEscalationRows(Protocol):
    """The durable escalation rows of stops; one row per row id."""

    def append_if_absent(self, row: ModelStopEscalationRow) -> bool:
        """Append the row unless its id is already there; True when this call wrote it."""
        ...

    def read(self, row_id: str) -> ModelStopEscalationRow | None:
        """The durable row with this id, read back."""
        ...

    def attach_receipt(self, row_id: str, receipt: ModelAlarmReceipt) -> None:
        """Record the alarm's delivery receipt on the row with this id."""
        ...


@runtime_checkable
class ProtocolStopAlarm(Protocol):
    """The alarm channel. ``deliver`` is idempotent on the row id: a repeat returns the first receipt."""

    def deliver(self, row: ModelStopEscalationRow) -> ModelAlarmReceipt:
        """Deliver one alarm for the row; raises when the channel did not accept it."""
        ...
