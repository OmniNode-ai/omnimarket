# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus a lab-work caller needs: one that can read a topic with no consumer group.

The caller (``onex lab-work run`` and ``hosts``) reads one capacity window and
one receipt and leaves. A consumer group per call left about 15,400 Empty
groups on the CI bus, whose offsets log the broker could not replay inside its
memory budget (OMN-21764). ``EventBusKafka.subscribe_ungrouped`` (OMN-17427)
reads a topic with no group, and the node contract declares that the caller
uses it (``caller_subscription.mode: ungrouped``).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus


@runtime_checkable
class ProtocolUngroupedLabRunBus(ProtocolLabRunBus, Protocol):
    """A lab-run bus that can also read a topic with no consumer group."""

    async def subscribe_ungrouped(
        self, topic: str, on_message: Callable[[Any], Awaitable[None]]
    ) -> Callable[[], Awaitable[None]]: ...


def require_ungrouped(bus: ProtocolLabRunBus) -> ProtocolUngroupedLabRunBus:
    """Return ``bus`` typed for group-less reads, or refuse a bus that cannot do them."""
    if not isinstance(bus, ProtocolUngroupedLabRunBus):
        raise TypeError(
            f"{type(bus).__name__} has no subscribe_ungrouped: a lab-work caller joins "
            "no consumer group, so it needs EventBusKafka or the in-memory infra bus"
        )
    return bus
