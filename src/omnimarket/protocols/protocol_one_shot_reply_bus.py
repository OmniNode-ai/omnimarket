# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A one-shot reply read that leaves no broker consumer group."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from omnimarket.protocols.protocol_lab_run_bus import MessageCallback, ProtocolLabRunBus


@runtime_checkable
class ProtocolOneShotReplyBus(ProtocolLabRunBus, Protocol):
    """A bus that can read a reply with no consumer group (OMN-17427).

    ``subscribe_ungrouped`` reads ``topic`` from its log end at the moment it
    returns, with no group and no committed offsets, so a caller that reads one
    reply and leaves leaves nothing on the broker. The in-memory bus and the
    Kafka bus (omnibase_infra) both answer it. Long-running hosts that need
    their position committed keep ``subscribe`` with a group.
    """

    async def subscribe_ungrouped(
        self, topic: str, on_message: MessageCallback
    ) -> Callable[[], Awaitable[None]]: ...
