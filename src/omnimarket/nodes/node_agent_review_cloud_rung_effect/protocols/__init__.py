# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of the cloud rung: the review-only cloud lane and the bus its delegation event travels on."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from ..models import (
    ModelCloudEmitReceipt,
    ModelCloudLaneRun,
    ModelCloudRungConfig,
    ModelCloudVoteRequest,
)


class ProtocolCloudLaneCaller(Protocol):
    """Run one review-only cloud lane with this brief and return what its receipt holds."""

    async def run(
        self,
        brief: str,
        lane: str,
        request: ModelCloudVoteRequest,
        config: ModelCloudRungConfig,
    ) -> ModelCloudLaneRun: ...


class ProtocolCloudRungPublisher(Protocol):
    """What the delegation event sink needs of a bus: one publish."""

    async def publish(self, topic: str, key: bytes | None, value: bytes) -> object: ...


class ProtocolCloudRungBus(ProtocolCloudRungPublisher, Protocol):
    """The in-memory or Redpanda bus binding, with the subscription a test observes the event through."""

    async def subscribe_ungrouped(
        self,
        topic: str,
        on_message: Callable[[ModelEventMessage], Awaitable[None]],
    ) -> Callable[[], Awaitable[None]]: ...


class ProtocolDelegationEventSink(Protocol):
    """Put one delegation terminal on the bus; the receipt says whether it was published."""

    async def emit(
        self, topic: str, key: str, payload: dict[str, object]
    ) -> ModelCloudEmitReceipt: ...


__all__ = [
    "ProtocolCloudLaneCaller",
    "ProtocolCloudRungBus",
    "ProtocolCloudRungPublisher",
    "ProtocolDelegationEventSink",
]
