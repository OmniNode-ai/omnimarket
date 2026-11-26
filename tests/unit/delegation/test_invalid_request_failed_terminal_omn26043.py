# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation request the wire model refuses still ends with a typed failed terminal (OMN-26043).

A request whose task class the contract does not declare (or whose payload fails the wire
model) used to be answered INVALID_MESSAGE with no output event: the gateway row stayed
``published`` and never reached ``failed``. The dispatcher now emits the failed terminal,
carrying the correlation id and tenant of the refused request and a typed class and code.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_infra.enums import EnumDispatchStatus

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_request import (
    DispatcherDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)

pytestmark = [pytest.mark.unit]

# The machine-readable grammar the gateway terminal writer and the dead-run check read.
_GRAMMAR = re.compile(
    r"^(?P<cls>[A-Za-z_]\w*(Error|Exception)): (?P<code>ONEX_[A-Z0-9_]+)$"
)


def _envelope(payload: dict[str, object]) -> dict[str, object]:
    return {
        "payload": payload,
        "__bindings": {},
        "__debug_trace": {"correlation_id": str(uuid4())},
    }


def _payload(task_type: str, correlation_id: UUID, tenant: str) -> dict[str, object]:
    return {
        "correlation_id": str(correlation_id),
        "task_type": task_type,
        "prompt": "say ok",
        "tenant_id": tenant,
        "emitted_at": datetime.now(UTC).isoformat(),
    }


@pytest.mark.asyncio
async def test_unroutable_task_class_ends_in_typed_failed_terminal() -> None:
    cid, tenant = uuid4(), str(uuid4())
    dispatcher = DispatcherDelegationRequest(HandlerDelegationWorkflow(workflows={}))
    result = await dispatcher.handle(
        _envelope(_payload("omn19181_not_a_task_class", cid, tenant))
    )
    terminals = [
        e for e in result.output_events if isinstance(e, ModelDelegationFailed)
    ]
    assert len(terminals) == 1
    terminal = terminals[0]
    assert terminal.correlation_id == cid
    assert terminal.tenant_id == tenant
    assert terminal.model_used == "none"
    assert terminal.attempts_count == 0
    assert terminal.quality_passed is False
    assert terminal.terminal_failure_reason is not None
    assert _GRAMMAR.match(terminal.terminal_failure_reason)
    assert terminal.terminal_failure_reason == (
        "DelegationRequestInvalidError: ONEX_DELEGATION_REQUEST_INVALID"
    )
    assert result.status == EnumDispatchStatus.SUCCESS


@pytest.mark.asyncio
async def test_invalid_payload_field_ends_in_typed_failed_terminal() -> None:
    cid, tenant = uuid4(), str(uuid4())
    payload = _payload("document", cid, tenant)
    payload["prompt"] = 12345  # not a string
    dispatcher = DispatcherDelegationRequest(HandlerDelegationWorkflow(workflows={}))
    result = await dispatcher.handle(_envelope(payload))
    terminals = [
        e for e in result.output_events if isinstance(e, ModelDelegationFailed)
    ]
    assert len(terminals) == 1
    assert terminals[0].correlation_id == cid
    assert _GRAMMAR.match(terminals[0].terminal_failure_reason or "")
    assert terminals[0].terminal_failure_reason == (
        "DelegationRequestInvalidError: ONEX_DELEGATION_REQUEST_INVALID"
    )


@pytest.mark.asyncio
async def test_unreadable_correlation_id_stays_invalid_message_without_terminal() -> (
    None
):
    payload = _payload("omn19181_not_a_task_class", uuid4(), str(uuid4()))
    payload["correlation_id"] = "not-a-uuid"
    dispatcher = DispatcherDelegationRequest(HandlerDelegationWorkflow(workflows={}))
    result = await dispatcher.handle({"payload": payload, "__bindings": {}})
    assert not [e for e in result.output_events if isinstance(e, ModelDelegationFailed)]


@pytest.mark.asyncio
async def test_valid_request_still_routes() -> None:
    dispatcher = DispatcherDelegationRequest(HandlerDelegationWorkflow(workflows={}))
    result = await dispatcher.handle(
        _envelope(_payload("document", uuid4(), str(uuid4())))
    )
    assert result.status == EnumDispatchStatus.SUCCESS
    assert not [e for e in result.output_events if isinstance(e, ModelDelegationFailed)]


@pytest.mark.asyncio
async def test_dead_run_probe_task_class_ends_failed_with_typed_reason() -> None:
    """The dying task class the C16 and C28 probes submit ends failed, typed, with the reason."""
    from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
        DYING_TASK_TYPE,
    )
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
        INVALID_REQUEST_FAILURE_REASON,
    )

    cid = uuid4()
    dispatcher = DispatcherDelegationRequest(HandlerDelegationWorkflow(workflows={}))
    result = await dispatcher.handle(
        _envelope(_payload(DYING_TASK_TYPE, cid, str(uuid4())))
    )
    terminals = [
        e for e in result.output_events if isinstance(e, ModelDelegationFailed)
    ]
    assert len(terminals) == 1
    assert terminals[0].correlation_id == cid
    assert terminals[0].terminal_failure_reason == INVALID_REQUEST_FAILURE_REASON
