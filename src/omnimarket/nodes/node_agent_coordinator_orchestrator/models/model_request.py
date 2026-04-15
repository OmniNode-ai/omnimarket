# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2025 OmniNode Team
"""Agent Coordinator Request model for cross-agent memory coordination operations.

This module defines the request envelope used by the agent_coordinator_orchestrator
node to perform subscription and notification operations for cross-agent memory
coordination.

Delivery Mechanism:
    Notifications are published to Kafka. Agents consume events directly via
    consumer groups. No webhook delivery configuration is needed.

Example:
    >>> from omnimarket.nodes.node_agent_coordinator_orchestrator.models import (
    ...     ModelAgentCoordinatorRequest,
    ...     EnumAgentCoordinatorAction,
    ... )
    >>> request = ModelAgentCoordinatorRequest(
    ...     action=EnumAgentCoordinatorAction.SUBSCRIBE,
    ...     agent_id="agent_alpha",
    ...     topic="memory.item.created",
    ... )

.. versionadded:: 0.1.0
    Initial implementation for OMN-1393.

.. versionchanged:: 0.2.0
    Removed webhook delivery configuration. Notifications now use Kafka.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self
from uuid import UUID, uuid4

from omnimemory.models.subscription.model_notification_event import (
    ModelNotificationEvent,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

__all__ = ["EnumAgentCoordinatorAction", "ModelAgentCoordinatorRequest"]


class EnumAgentCoordinatorAction(StrEnum):
    """Actions supported by the agent coordinator orchestrator.

    Attributes:
        SUBSCRIBE: Register an agent's subscription to a memory topic.
        UNSUBSCRIBE: Remove an agent's subscription from a topic.
        LIST_SUBSCRIPTIONS: List all subscriptions for an agent.
        NOTIFY: Publish notification event to Kafka for subscriber consumption.
    """

    SUBSCRIBE = "subscribe"
    UNSUBSCRIBE = "unsubscribe"
    LIST_SUBSCRIPTIONS = "list_subscriptions"
    NOTIFY = "notify"


class ModelAgentCoordinatorRequest(BaseModel):
    """Request envelope for agent coordinator operations.

    This model encapsulates all parameters needed to perform subscription
    management and notification operations for cross-agent memory coordination.
    The action field determines which operation to execute, and other fields
    provide context-specific data for that operation.

    Validation rules per action:
        - subscribe: Requires agent_id, topic
        - unsubscribe: Requires agent_id, topic
        - list_subscriptions: Requires agent_id
        - notify: Requires topic, event

    Attributes:
        action: The coordination action to perform.
        agent_id: Unique identifier of the agent (required for subscribe/
            unsubscribe/list_subscriptions).
        topic: Memory topic pattern in format memory.<entity>.<event>
            (required for subscribe/unsubscribe/notify).
        event: Notification event to publish (required for notify).
        metadata: Optional metadata for the subscription (for subscribe).
        correlation_id: Request correlation ID for tracing.

    Example:
        >>> # Subscribe to memory changes
        >>> subscribe_request = ModelAgentCoordinatorRequest(
        ...     action=EnumAgentCoordinatorAction.SUBSCRIBE,
        ...     agent_id="agent_alpha",
        ...     topic="memory.item.created",
        ... )
        >>>
        >>> # Notify subscribers of a change (publishes to Kafka)
        >>> notify_request = ModelAgentCoordinatorRequest(
        ...     action=EnumAgentCoordinatorAction.NOTIFY,
        ...     topic="memory.item.created",
        ...     event=my_notification_event,
        ... )
        >>>
        >>> # List agent's subscriptions
        >>> list_request = ModelAgentCoordinatorRequest(
        ...     action=EnumAgentCoordinatorAction.LIST_SUBSCRIPTIONS,
        ...     agent_id="agent_alpha",
        ... )

    Raises:
        ValueError: If required fields for the action are missing.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    action: EnumAgentCoordinatorAction = Field(
        ...,
        description="The coordination action to perform",
    )

    agent_id: str | None = Field(
        default=None,
        description="Agent ID (required for subscribe/unsubscribe/list_subscriptions)",
    )

    topic: str | None = Field(
        default=None,
        description="Topic pattern (required for subscribe/unsubscribe/notify)",
    )

    event: ModelNotificationEvent | None = Field(
        default=None,
        description="Notification event to publish to Kafka (required for notify)",
    )

    metadata: dict[str, str] | None = Field(
        default=None,
        description="Optional metadata for the subscription (for subscribe)",
    )

    correlation_id: UUID = Field(
        default_factory=uuid4,
        description="Request correlation ID for tracing",
    )

    @model_validator(mode="after")
    def validate_action_fields(self) -> Self:
        """Validate that required fields are present for each action type.

        Validation rules:
            - subscribe: requires agent_id, topic
            - unsubscribe: requires agent_id, topic
            - list_subscriptions: requires agent_id
            - notify: requires topic, event

        Returns:
            Self: The validated instance.

        Raises:
            ValueError: If required fields are missing for the action.
        """
        if self.action == EnumAgentCoordinatorAction.SUBSCRIBE:
            missing = []
            if self.agent_id is None:
                missing.append("agent_id")
            if self.topic is None:
                missing.append("topic")
            if missing:
                raise ValueError(
                    f"'subscribe' action requires fields: {', '.join(missing)}"
                )

        elif self.action == EnumAgentCoordinatorAction.UNSUBSCRIBE:
            missing = []
            if self.agent_id is None:
                missing.append("agent_id")
            if self.topic is None:
                missing.append("topic")
            if missing:
                raise ValueError(
                    f"'unsubscribe' action requires fields: {', '.join(missing)}"
                )

        elif self.action == EnumAgentCoordinatorAction.LIST_SUBSCRIPTIONS:
            if self.agent_id is None:
                raise ValueError("'list_subscriptions' action requires 'agent_id'")

        elif self.action == EnumAgentCoordinatorAction.NOTIFY:
            missing = []
            if self.topic is None:
                missing.append("topic")
            if self.event is None:
                missing.append("event")
            if missing:
                raise ValueError(
                    f"'notify' action requires fields: {', '.join(missing)}"
                )

            # Topic consistency check: event.topic must match request topic
            if (
                self.event is not None
                and self.topic is not None
                and self.event.topic != self.topic
            ):
                raise ValueError(
                    f"Event topic mismatch: event.topic='{self.event.topic}' "
                    f"does not match topic='{self.topic}'"
                )

        return self
