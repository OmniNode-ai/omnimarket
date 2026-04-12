# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Agent Coordinator Orchestrator - ONEX Node (Core 8 Foundation).

Cross-agent memory coordination and sharing through subscription management
and Kafka-based notification delivery.

Node Type: ORCHESTRATOR
- Workflow coordination for agent subscriptions
- Cross-agent notification publishing to Kafka
- Agents consume events directly via consumer groups

Delivery Mechanism:
    Notifications are published to Kafka event bus. Internal agents consume
    events directly via consumer groups. If external (non-Kafka) delivery
    is needed in the future, implement a WebhookEmitterEffect node.

ONEX 4.0 Declarative Pattern:
    This node follows the fully declarative ONEX pattern:
    - contract.yaml defines the node type, inputs, outputs, and dependencies
    - Business logic lives in HandlerSubscription (omnimemory.handlers)
    - No node.py class needed - the contract IS the node definition

Models::

    from omnimarket.nodes.node_agent_coordinator_orchestrator import (
        EnumAgentCoordinatorAction,
        ModelAgentCoordinatorRequest,
        ModelAgentCoordinatorResponse,
    )

Handler Integration::

    import os
    from omnibase_core.container import ModelONEXContainer
    from omnimemory.handlers import (
        HandlerSubscription,
        ModelHandlerSubscriptionConfig,
    )

    container = ModelONEXContainer()
    config = ModelHandlerSubscriptionConfig(
        db_dsn=os.environ["OMNIMEMORY_DB_URL"],
        valkey_host=os.environ["VALKEY_HOST"],
        kafka_bootstrap_servers=os.getenv("KAFKA_BOOTSTRAP_SERVERS"),
    )
    handler = HandlerSubscription(container)
    await handler.initialize(config)

    # Subscribe an agent
    subscription = await handler.subscribe(
        agent_id="agent_123",
        topic="memory.item.created",
    )

    # Notify subscribers (publishes to Kafka)
    subscriber_count = await handler.notify(topic, event)

.. versionadded:: 0.1.0
    Initial implementation for OMN-1393.

.. versionchanged:: 0.2.0
    Migrated to ONEX 4.0 fully declarative pattern.
    Replaced webhook delivery with Kafka event bus.
"""

from .models import (
    EnumAgentCoordinatorAction,
    ModelAgentCoordinatorRequest,
    ModelAgentCoordinatorResponse,
)

__all__ = [
    # Models
    "EnumAgentCoordinatorAction",
    "ModelAgentCoordinatorRequest",
    "ModelAgentCoordinatorResponse",
]
