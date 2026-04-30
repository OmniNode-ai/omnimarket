"""Tests for typed Pattern B broker envelopes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_pattern_b_broker.models import (
    EnumPatternBBrokerAclDecision,
    EnumPatternBBrokerEventType,
    EnumPatternBBrokerOriginator,
    EnumPatternBBrokerRecipient,
    EnumPatternBBrokerState,
    EnumPatternBBrokerTerminalStatus,
    ModelPatternBBrokerAclInput,
    ModelPatternBBrokerAclResult,
    ModelPatternBBrokerDispatchRequest,
    ModelPatternBBrokerTerminalEvent,
    ModelPatternBBrokerWaitPolicy,
)


@pytest.mark.unit
def test_dispatch_request_coerces_event_type_to_enum() -> None:
    request = ModelPatternBBrokerDispatchRequest(
        correlation_id=uuid4(),
        event_type="dispatch_requested",
        state="accepted",
        originator="omnimarket",
        recipient="omniclaude",
        skill_name="session-orchestrator",
        payload={"ticket_id": "OMN-10438"},
    )

    assert request.event_type is EnumPatternBBrokerEventType.dispatch_requested
    assert request.state is EnumPatternBBrokerState.accepted
    assert request.originator is EnumPatternBBrokerOriginator.omnimarket
    assert request.recipient is EnumPatternBBrokerRecipient.omniclaude


@pytest.mark.unit
def test_dispatch_request_rejects_non_dispatch_event_type() -> None:
    with pytest.raises(ValidationError, match="dispatch_requested"):
        ModelPatternBBrokerDispatchRequest(
            correlation_id=uuid4(),
            event_type=EnumPatternBBrokerEventType.terminal_completed,
            originator=EnumPatternBBrokerOriginator.omnimarket,
            recipient=EnumPatternBBrokerRecipient.omniclaude,
            skill_name="session-orchestrator",
        )


@pytest.mark.unit
def test_terminal_event_requires_terminal_enum_values() -> None:
    terminal = ModelPatternBBrokerTerminalEvent(
        request_id=uuid4(),
        correlation_id=uuid4(),
        event_type="terminal_completed",
        state="completed",
        status="completed",
        result={"summary": "done"},
    )

    assert terminal.event_type is EnumPatternBBrokerEventType.terminal_completed
    assert terminal.state is EnumPatternBBrokerState.completed
    assert terminal.status is EnumPatternBBrokerTerminalStatus.completed


@pytest.mark.unit
def test_terminal_event_rejects_dispatch_event_type() -> None:
    with pytest.raises(ValidationError, match="terminal enum value"):
        ModelPatternBBrokerTerminalEvent(
            request_id=uuid4(),
            correlation_id=uuid4(),
            event_type=EnumPatternBBrokerEventType.dispatch_requested,
            state=EnumPatternBBrokerState.completed,
            status=EnumPatternBBrokerTerminalStatus.completed,
        )


@pytest.mark.unit
def test_terminal_event_rejects_mismatched_outcome_enums() -> None:
    with pytest.raises(ValidationError, match="same outcome"):
        ModelPatternBBrokerTerminalEvent(
            request_id=uuid4(),
            correlation_id=uuid4(),
            event_type=EnumPatternBBrokerEventType.terminal_completed,
            state=EnumPatternBBrokerState.failed,
            status=EnumPatternBBrokerTerminalStatus.failed,
        )


@pytest.mark.unit
def test_wait_policy_requires_terminal_statuses() -> None:
    with pytest.raises(ValidationError, match="at least one status"):
        ModelPatternBBrokerWaitPolicy(terminal_statuses=())


@pytest.mark.unit
def test_acl_models_use_enums_for_decision_boundary() -> None:
    acl_input = ModelPatternBBrokerAclInput(
        originator="omnimarket",
        recipient="omniclaude",
        skill_name="session-orchestrator",
    )
    acl_result = ModelPatternBBrokerAclResult(
        decision="allow",
        reason="originator and recipient are contract-allowed",
        matched_rule="default-allowlist",
    )

    assert acl_input.originator is EnumPatternBBrokerOriginator.omnimarket
    assert acl_input.recipient is EnumPatternBBrokerRecipient.omniclaude
    assert acl_result.decision is EnumPatternBBrokerAclDecision.allow
