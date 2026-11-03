# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers of node_throughput_tick_decision_compute.

The scheduled-fire handler imports omnibase_infra's runtime models (about 2.5 s of a cold
start); the decision handler does not need them, so the export resolves on first access.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .handler_throughput_tick_scheduled_fire import (
        HandlerThroughputTickScheduledFire,
    )

__all__ = ["HandlerThroughputTickScheduledFire"]


def __getattr__(name: str) -> Any:
    if name == "HandlerThroughputTickScheduledFire":
        from .handler_throughput_tick_scheduled_fire import (
            HandlerThroughputTickScheduledFire,
        )

        return HandlerThroughputTickScheduledFire
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
