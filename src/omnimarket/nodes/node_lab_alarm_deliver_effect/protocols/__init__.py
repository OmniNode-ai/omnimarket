# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_lab_alarm_deliver_effect: the alert channel, the ledger and the episode state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelAlarmSendReceipt,
)


class ProtocolAlarmChannel(Protocol):
    def send(self, *, category: str, text: str) -> ModelAlarmSendReceipt:
        """Post one alarm message; a failure is a receipt that says so, never a silent return."""
        ...


class LedgerAppendError(Exception):
    """The ledger host refused or could not take the row; the message names why."""


class ProtocolAlarmLedger(Protocol):
    def append(self, *, request_id: str, row: str) -> str:
        """Append one row under ``request_id``. Returns ``accepted`` or ``duplicate``; raises LedgerAppendError."""
        ...


class ProtocolDeliveryStateStore(Protocol):
    def load(self) -> dict[str, Any]:
        """The episode state (``active``, ``ledger_last_row``, ``ledger_pending``), empty when there is none."""
        ...

    def save(self, state: Mapping[str, Any]) -> None: ...


__all__ = [
    "LedgerAppendError",
    "ProtocolAlarmChannel",
    "ProtocolAlarmLedger",
    "ProtocolDeliveryStateStore",
]
