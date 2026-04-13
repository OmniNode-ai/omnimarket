# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Intent Event Consumer Effect node — consumes intent-classified events and routes to storage.

Migrated from omnimemory to omnimarket (OMN-8300).
"""

from omnimarket.nodes.node_intent_event_consumer_effect.handler_intent_event_consumer import (
    HandlerIntentEventConsumer,
)
from omnimarket.nodes.node_intent_event_consumer_effect.models import (
    ModelIntentEventConsumerConfig,
    ModelIntentEventConsumerHealth,
)

__all__ = [
    "HandlerIntentEventConsumer",
    "ModelIntentEventConsumerConfig",
    "ModelIntentEventConsumerHealth",
]
