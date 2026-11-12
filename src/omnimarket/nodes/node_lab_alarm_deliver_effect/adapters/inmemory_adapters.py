# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory backend, the default: recorders for the channel and the ledger, and a dict for the episode state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.models.alarm_send_receipt import ModelAlarmSendReceipt
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import LedgerAppendError


class InMemoryAlarmChannel:
    """Records every post; ``refuse`` makes the next posts fail with that reason."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, str]] = []
        self.refuse: str | None = None

    def send(self, *, category: str, text: str) -> ModelAlarmSendReceipt:
        if self.refuse is not None:
            return ModelAlarmSendReceipt(delivered=False, failure=self.refuse)
        self.posts.append((category, text))
        return ModelAlarmSendReceipt(
            delivered=True, message_ref=f"inmemory-{len(self.posts)}"
        )


class InMemoryAlarmLedger:
    """Records every row; ``refuse`` makes the append raise, and a request id seen before answers duplicate."""

    def __init__(self) -> None:
        self.rows: list[tuple[str, str]] = []
        self.refuse: str | None = None

    def append(self, *, request_id: str, row: str) -> str:
        if self.refuse is not None:
            raise LedgerAppendError(self.refuse)
        if any(seen == request_id for seen, _ in self.rows):
            return "duplicate"
        self.rows.append((request_id, row))
        return "accepted"


class InMemoryDeliveryState:
    def __init__(self, state: Mapping[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = dict(state or {})

    def load(self) -> dict[str, Any]:
        return {key: dict(value) for key, value in self.state.items()}

    def save(self, state: Mapping[str, Any]) -> None:
        self.state = {key: dict(value) for key, value in state.items()}


__all__ = ["InMemoryAlarmChannel", "InMemoryAlarmLedger", "InMemoryDeliveryState"]
