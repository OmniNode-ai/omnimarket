# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain tests for node_agent_coordinator_orchestrator.

Verifies request/response model contracts for subscribe, unsubscribe,
list_subscriptions, and notify actions. Zero infra required.

Related: OMN-8301 (Wave 5 migration), OMN-1393
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_agent_coordinator_orchestrator import (
    EnumAgentCoordinatorAction,
    ModelAgentCoordinatorRequest,
    ModelAgentCoordinatorResponse,
)


@pytest.mark.unit
class TestAgentCoordinatorOrchestratorGoldenChain:
    """Golden chain: agent coordinator orchestrator model contracts."""

    def test_subscribe_request_valid(self) -> None:
        """Subscribe action requires agent_id and topic."""
        req = ModelAgentCoordinatorRequest(
            action=EnumAgentCoordinatorAction.SUBSCRIBE,
            agent_id="agent_alpha",
            topic="memory.item.created",
        )
        assert req.action == EnumAgentCoordinatorAction.SUBSCRIBE
        assert req.agent_id == "agent_alpha"
        assert req.topic == "memory.item.created"

    def test_unsubscribe_request_valid(self) -> None:
        """Unsubscribe action requires agent_id and topic."""
        req = ModelAgentCoordinatorRequest(
            action=EnumAgentCoordinatorAction.UNSUBSCRIBE,
            agent_id="agent_beta",
            topic="memory.item.deleted",
        )
        assert req.action == EnumAgentCoordinatorAction.UNSUBSCRIBE

    def test_list_subscriptions_request_valid(self) -> None:
        """List subscriptions action requires only agent_id."""
        req = ModelAgentCoordinatorRequest(
            action=EnumAgentCoordinatorAction.LIST_SUBSCRIPTIONS,
            agent_id="agent_gamma",
        )
        assert req.action == EnumAgentCoordinatorAction.LIST_SUBSCRIPTIONS
        assert req.agent_id == "agent_gamma"
        assert req.topic is None

    def test_notify_request_valid(self) -> None:
        """Notify action requires topic and event."""
        from omnimemory.models.subscription.model_notification_event import (
            ModelNotificationEvent,
        )
        from omnimemory.models.subscription.model_notification_event_payload import (
            ModelNotificationEventPayload,
        )

        payload = ModelNotificationEventPayload(
            entity_type="memory",
            entity_id="mem_123",
            action="created",
        )
        event = ModelNotificationEvent(
            event_id="evt_abc",
            topic="memory.item.updated",
            payload=payload,
        )
        req = ModelAgentCoordinatorRequest(
            action=EnumAgentCoordinatorAction.NOTIFY,
            topic="memory.item.updated",
            event=event,
        )
        assert req.action == EnumAgentCoordinatorAction.NOTIFY
        assert req.topic == "memory.item.updated"
        assert req.event is not None

    def test_enum_values_match_routing_keys(self) -> None:
        """Enum string values match contract.yaml routing keys."""
        assert EnumAgentCoordinatorAction.SUBSCRIBE.value == "subscribe"
        assert EnumAgentCoordinatorAction.UNSUBSCRIBE.value == "unsubscribe"
        assert (
            EnumAgentCoordinatorAction.LIST_SUBSCRIPTIONS.value == "list_subscriptions"
        )
        assert EnumAgentCoordinatorAction.NOTIFY.value == "notify"

    def test_all_four_actions_exist(self) -> None:
        """All 4 routing actions are declared in the enum."""
        actions = {a.value for a in EnumAgentCoordinatorAction}
        assert actions == {"subscribe", "unsubscribe", "list_subscriptions", "notify"}

    def test_response_importable(self) -> None:
        """ModelAgentCoordinatorResponse is importable from omnimarket.nodes."""
        assert ModelAgentCoordinatorResponse is not None

    def test_node_importable(self) -> None:
        """node_agent_coordinator_orchestrator is importable from omnimarket.nodes."""
        import omnimarket.nodes.node_agent_coordinator_orchestrator as node

        assert node is not None

    def test_extra_fields_rejected(self) -> None:
        """Extra fields in request raise ValidationError (extra=forbid)."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            ModelAgentCoordinatorRequest(  # type: ignore[call-overload]
                action=EnumAgentCoordinatorAction.SUBSCRIBE,
                agent_id="x",
                topic="y",
                unknown_field="bad",
            )
