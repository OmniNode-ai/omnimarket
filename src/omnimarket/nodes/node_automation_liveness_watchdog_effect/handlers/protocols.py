# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The three ends the watchdog delivery plugs into (OMN-20806)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from omnimarket.nodes.node_automation_liveness_watchdog_effect.models.model_watchdog_delivery import (
    ModelAlarmSendReceipt,
)


class ProtocolAlarmSender(Protocol):
    """Post one alarm message; a failure is a receipt, never a silent return."""

    def send(
        self, *, episode_id: UUID, category: str, text: str
    ) -> ModelAlarmSendReceipt: ...


class ProtocolChannelHistory(Protocol):
    """Find the message that already carries an episode id, or None.

    Raises when the channel history cannot be read; the caller then sends again
    (delivery is at least once).
    """

    def find_message_ref(self, episode_id: UUID) -> str | None: ...


class ProtocolEscalationRoute(Protocol):
    """The route outside Slack; returns its receipt (an issue reference)."""

    def escalate(self, *, episode_id: UUID, text: str) -> str: ...


class ProtocolAlarmEventSink(Protocol):
    """The broker end: publish one event or raise when the broker will not take it."""

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None: ...


class UnwiredEventSink:
    """Stands in until a broker sink is wired; every publish fails loudly."""

    def publish(
        self, topic: str, payload: dict[str, object], *, key: str, event_id: str
    ) -> None:
        raise RuntimeError("no event sink is wired")


class UnwiredChannelHistory:
    """Stands in until a Slack history reader is wired; every read fails loudly.

    The handler treats an unreadable history as unknown and sends again, so an
    unwired reader costs duplicates after a crash, never a lost alarm.
    """

    def find_message_ref(self, episode_id: UUID) -> str | None:
        raise RuntimeError("no alert-channel history reader is wired")


class UnwiredEscalationRoute:
    """Stands in until the external route is wired; every escalation fails loudly."""

    def escalate(self, *, episode_id: UUID, text: str) -> str:
        raise RuntimeError("no external escalation route is wired")
