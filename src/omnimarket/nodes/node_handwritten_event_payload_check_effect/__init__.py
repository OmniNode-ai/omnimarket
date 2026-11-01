# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handwritten event-payload check effect node."""

from .handlers.handler_handwritten_event_payload_check import (
    HandlerHandwrittenEventPayloadCheck,
)


class NodeHandwrittenEventPayloadCheckEffect(HandlerHandwrittenEventPayloadCheck):
    """ONEX entrypoint for the handwritten event-payload fixture gate."""


__all__ = [
    "HandlerHandwrittenEventPayloadCheck",
    "NodeHandwrittenEventPayloadCheckEffect",
]
