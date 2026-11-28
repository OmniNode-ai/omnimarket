# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The channel behind the post policy: every message the delivery sends is decided first (OMN-24562)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from omnimarket.handlers.alarm_post_policy import AlarmPostGate
from omnimarket.models.alarm_post_policy import EnumPostAction, ModelPostDecision
from omnimarket.models.alarm_send_receipt import ModelAlarmSendReceipt
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    ProtocolAlarmChannel,
    ProtocolDeliveryStateStore,
)

#: Records what the gate decided for a message that did not post (the bus reading).
ReadingSink = Callable[[ModelPostDecision, str], None]
#: Raises the ticket for a stale episode; True when it was raised.
TicketRaiser = Callable[[ModelPostDecision], bool]


class PolicyGatedChannel:
    """Sends only what the policy lets through; a suppressed message is a delivered no-op that leaves a reading."""

    def __init__(
        self,
        *,
        inner: ProtocolAlarmChannel,
        gate: AlarmPostGate,
        state: ProtocolDeliveryStateStore,
        clock: Callable[[], datetime],
        reading: ReadingSink,
        ticket: TicketRaiser,
    ) -> None:
        self._inner = inner
        self._gate = gate
        self._state = state
        self._clock = clock
        self._reading = reading
        self._ticket = ticket

    def send(self, *, category: str, text: str) -> ModelAlarmSendReceipt:
        gate_state: dict[str, Any] = self._state.load()
        decision = self._gate.decide(text, now=self._clock(), state=gate_state)
        if decision.action is EnumPostAction.TICKET:
            if not self._ticket(decision):
                gate_state["open"][decision.key].pop("ticketed", None)
            self._state.save(gate_state)
            self._reading(decision, text)
            return ModelAlarmSendReceipt(
                delivered=True, message_ref=f"policy:{decision.action.value}"
            )
        if decision.action is EnumPostAction.SUPPRESS:
            self._state.save(gate_state)
            self._reading(decision, text)
            return ModelAlarmSendReceipt(
                delivered=True, message_ref=f"policy:suppressed:{decision.reason}"
            )
        receipt = self._inner.send(category=category, text=decision.text)
        if receipt.delivered:
            self._state.save(gate_state)
        return receipt


__all__ = ["PolicyGatedChannel", "ReadingSink", "TicketRaiser"]
