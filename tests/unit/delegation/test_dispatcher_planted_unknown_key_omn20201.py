# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests proving delegation dispatchers reject undeclared wire payload keys (OMN-20201).

omnibase_core ModelInvocationCommand and ModelAgentTaskLifecycleEvent now set
extra="forbid", so a dispatch payload carrying a key the model does not declare
must be rejected with EnumDispatchStatus.INVALID_MESSAGE, while the same
payload without the key must dispatch with SUCCESS.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from omnibase_core.enums import EnumInvocationKind
from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_infra.enums import EnumDispatchStatus

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_agent_task_lifecycle import (
    DispatcherAgentTaskLifecycle,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_invocation_command import (
    DispatcherInvocationCommand,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)

pytestmark = [pytest.mark.unit]


def _make_dispatch_envelope(payload: dict[str, object]) -> dict[str, object]:
    return {
        "payload": payload,
        "__bindings": {},
        "__debug_trace": {"correlation_id": str(uuid4())},
    }


def _invocation_command_payload() -> dict[str, object]:
    task_id = uuid4()
    correlation_id = uuid4()
    return {
        "task_id": str(task_id),
        "correlation_id": str(correlation_id),
        "invocation_kind": EnumInvocationKind.AGENT.value,
        "agent_protocol": EnumAgentProtocol.A2A.value,
        "target_ref": "agent://remote",
        "model_backend": None,
        "payload": {},
    }


def _lifecycle_event_payload() -> dict[str, object]:
    return {
        "task_id": str(uuid4()),
        "correlation_id": str(uuid4()),
        "lifecycle_type": EnumAgentTaskLifecycleType.SUBMITTED.value,
        "occurred_at": datetime.now(UTC).isoformat(),
        "remote_task_handle": "remote-1",
    }


@pytest.mark.asyncio
async def test_invocation_command_planted_key_is_invalid_message() -> None:
    payload = _invocation_command_payload()
    payload["unexpected_field"] = "x"
    dispatcher = DispatcherInvocationCommand(
        HandlerDelegationWorkflow(), event_bus=None
    )

    result = await dispatcher.handle(_make_dispatch_envelope(payload))

    assert result.status == EnumDispatchStatus.INVALID_MESSAGE
    assert result.error_message is not None
    assert "unexpected_field" in result.error_message


@pytest.mark.asyncio
async def test_invocation_command_clean_payload_succeeds() -> None:
    dispatcher = DispatcherInvocationCommand(
        HandlerDelegationWorkflow(), event_bus=None
    )

    result = await dispatcher.handle(
        _make_dispatch_envelope(_invocation_command_payload())
    )

    assert result.status == EnumDispatchStatus.SUCCESS


@pytest.mark.asyncio
async def test_agent_task_lifecycle_planted_key_is_invalid_message() -> None:
    payload = _lifecycle_event_payload()
    payload["unexpected_field"] = "x"
    dispatcher = DispatcherAgentTaskLifecycle(
        HandlerDelegationWorkflow(), event_bus=None
    )

    result = await dispatcher.handle(_make_dispatch_envelope(payload))

    assert result.status == EnumDispatchStatus.INVALID_MESSAGE
    assert result.error_message is not None
    assert "unexpected_field" in result.error_message


@pytest.mark.asyncio
async def test_agent_task_lifecycle_clean_payload_succeeds() -> None:
    dispatcher = DispatcherAgentTaskLifecycle(
        HandlerDelegationWorkflow(), event_bus=None
    )

    result = await dispatcher.handle(
        _make_dispatch_envelope(_lifecycle_event_payload())
    )

    assert result.status == EnumDispatchStatus.SUCCESS
