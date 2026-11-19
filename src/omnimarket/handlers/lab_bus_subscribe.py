# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Subscribe on the lab bus, stating where a fresh consumer group starts."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from omnimarket.delegated_test_loop.lab_run_bus import ProtocolLabRunBus


async def subscribe_with_offset(
    bus: ProtocolLabRunBus,
    topic: str,
    on_message: Callable[[Any], Awaitable[None]],
    group_id: str,
    offset_reset: str,
) -> Callable[[], Awaitable[None]]:
    """Subscribe; state where a fresh group starts when the bus can say so."""
    subscribe: Callable[..., Awaitable[Callable[[], Awaitable[None]]]] = bus.subscribe
    extra = (
        {"auto_offset_reset": offset_reset}
        if "auto_offset_reset" in inspect.signature(bus.subscribe).parameters
        else {}
    )
    return await subscribe(topic, on_message=on_message, group_id=group_id, **extra)
