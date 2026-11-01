# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_capture_silence_watch_effect handlers."""

from collections.abc import Awaitable, Callable

from omnimarket.nodes.node_capture_silence_watch_effect.handlers.handler_capture_silence_watch import (
    HandlerCaptureSilenceWatch,
)


def build_handler_capture_silence_watch(
    *,
    event_publisher: Callable[[str, bytes], Awaitable[None] | None] | None,
    host: str | None,
) -> HandlerCaptureSilenceWatch:
    """Build the timer handler with the supplied publisher and host."""
    return HandlerCaptureSilenceWatch(event_publisher=event_publisher, host=host)
