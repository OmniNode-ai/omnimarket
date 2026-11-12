# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Publish one census of a bus lane's consumer groups as an event (OMN-23992).

The guard effect publishes one census per tick so the sibling reducer has a producer for the
census topic. A census that could not be read is never published as a census: a listing that
raises or is empty raises ``ConsumerGroupCensusUnreadableError`` whose message names only the
exception class, never the client's error text.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.models.consumer_group_guard import ModelConsumerGroupGuardRequest
from omnimarket.models.consumer_group_state import (
    ModelGroupCensusEvent,
)
from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_guard import (
    list_group_states,
)
from omnimarket.nodes.node_runtime_coverage_effect.handlers.handler_runtime_coverage import (
    resolve_broker,
)


class ConsumerGroupCensusUnreadableError(RuntimeError):
    """A census that could not be read: the listing raised or was empty."""


class HandlerConsumerGroupCensusEvent:
    """Produce one census event of a bus lane's consumer groups."""

    def __init__(
        self,
        group_lister: Callable[[Mapping[str, str]], dict[str, str]] | None = None,
        broker_resolver: Callable[[str], tuple[str, str, dict[str, str]]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._list = group_lister or list_group_states
        self._resolve = broker_resolver or resolve_broker
        self._clock = clock

    async def handle(
        self, request: ModelConsumerGroupGuardRequest
    ) -> ModelGroupCensusEvent:
        try:
            _broker, _environment, config = self._resolve(request.bus_lane)
            groups = await asyncio.to_thread(self._list, config)
        except Exception as exc:
            # Error text from a client may carry connection credentials: keep the class only.
            raise ConsumerGroupCensusUnreadableError(type(exc).__name__) from None
        if not groups:
            raise ConsumerGroupCensusUnreadableError("an empty listing is not a census")
        return ModelGroupCensusEvent(
            correlation_id=uuid4(),
            bus_lane=request.bus_lane,
            observed_at=self._clock(),
            groups=groups,
        )


__all__: list[str] = [
    "ConsumerGroupCensusUnreadableError",
    "HandlerConsumerGroupCensusEvent",
]
