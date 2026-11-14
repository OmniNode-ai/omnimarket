# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: wired by handler_routing in contract.yaml (consumer_group_census_event_effect), loaded by the runtime from the contract
"""Publish one census of a bus lane's consumer groups as an event (OMN-23992).

The guard effect publishes one census per tick so the sibling reducer has a producer for the
census topic. A census that could not be read is never published as a census: a listing that
raises or is empty raises ``ConsumerGroupCensusUnreadableError`` whose message names only the
exception class, never the client's error text.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from uuid import uuid4

from omnimarket.models.consumer_group_guard import ModelConsumerGroupGuardRequest
from omnimarket.models.consumer_group_state import (
    ModelGroupCensusEvent,
)
from omnimarket.nodes.node_consumer_group_guard_effect.handlers.handler_consumer_group_guard import (
    lag_group_patterns,
    list_group_states,
    read_group_lag,
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
        lag_reader: Callable[[Mapping[str, str], list[str]], dict[str, int]]
        | None = None,
        lag_patterns: tuple[str, ...] | None = None,
    ) -> None:
        self._list = group_lister or list_group_states
        self._resolve = broker_resolver or resolve_broker
        self._clock = clock
        self._read_lag = lag_reader or read_group_lag
        self._lag_patterns = lag_patterns

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
        patterns = [
            re.compile(p)
            for p in (
                self._lag_patterns
                if self._lag_patterns is not None
                else lag_group_patterns()
            )
        ]
        watched = sorted(
            name
            for name, state in groups.items()
            if state != "EMPTY" and any(p.search(name) for p in patterns)
        )
        lag: dict[str, int] = {}
        if watched:
            try:
                lag = await asyncio.to_thread(self._read_lag, config, watched)
            except Exception:
                # The lag reading is secondary to the census: a failed read leaves every watched group's lag unread.
                lag = {}
        return ModelGroupCensusEvent(
            correlation_id=uuid4(),
            bus_lane=request.bus_lane,
            observed_at=self._clock(),
            groups=groups,
            lag={name: value for name, value in lag.items() if name in groups},
        )


__all__: list[str] = [
    "ConsumerGroupCensusUnreadableError",
    "HandlerConsumerGroupCensusEvent",
]
