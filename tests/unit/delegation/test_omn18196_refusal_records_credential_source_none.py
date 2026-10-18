# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation refused for want of a credential records ``credential_source=none``.

OMN-18196 criterion 2 says a delegation that ran on the customer's key records
``customer_key``, one that ran on a house credential records ``house``, and one
that was refused for want of a credential records ``none``.

The refusal is raised in the routing reducer, before the effect boundary
resolves anything, and reaches the orchestrator as a boundary failure terminal
on the routing leg. The orchestrator's terminal for that leg left
``credential_source`` unset, so the live receipt read ``null``. The refusal is a
fact the platform knows (no provider key was registered, so no credential
served the run); an unset value is indistinguishable from a terminal produced
before the field existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import EnumCredentialSource
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
    classify_boundary_failure,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.routing.customer_key_terminus import (
    CustomerKeyRefusedError,
    EnumCustomerKeyRefusalReason,
    EnumDelegationSurface,
    ModelCustomerKeyRefusal,
)

pytestmark = pytest.mark.unit

_ROUTING_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-routing-request.v1"  # onex-topic-allow: the routing leg's consumed topic, keyed in _BOUNDARY_FAILURE_LEGS


def _request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Reply with the single word: alive.",
        task_type="research",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )


def _refusal(
    correlation_id: UUID, reason: EnumCustomerKeyRefusalReason
) -> CustomerKeyRefusedError:
    return CustomerKeyRefusedError(
        ModelCustomerKeyRefusal(
            reason=reason,
            tenant_id="tenant-acme",
            task_type="research",
            surface=EnumDelegationSurface.CLOUD,
            correlation_id=correlation_id,
        )
    )


def _boundary_terminal(
    exc: Exception, correlation_id: UUID
) -> ModelBoundaryFailureTerminal:
    return classify_boundary_failure(
        exc,
        topic=_ROUTING_REQUEST_TOPIC,
        correlation_id=correlation_id,
        failure_reason=str(exc),
        failure_code=getattr(exc, "failure_code", None),
    )


async def _terminal_for(exc: Exception) -> ModelDelegationResult:
    correlation_id = getattr(exc, "refusal", None)
    correlation_id = (
        correlation_id.correlation_id if correlation_id is not None else uuid4()
    )
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(_request(correlation_id))
    events = await handler.handle(_boundary_terminal(exc, correlation_id))
    assert len(events) == 1
    terminal = events[0]
    assert isinstance(terminal, ModelDelegationResult)
    assert type(terminal).__name__ == "ModelDelegationFailed"
    return terminal


@pytest.mark.parametrize("reason", list(EnumCustomerKeyRefusalReason))
async def test_a_refused_delegation_records_none(
    reason: EnumCustomerKeyRefusalReason,
) -> None:
    terminal = await _terminal_for(_refusal(uuid4(), reason))
    assert terminal.credential_source is EnumCredentialSource.NONE


async def test_a_refused_delegation_does_not_claim_a_route_or_provider() -> None:
    terminal = await _terminal_for(
        _refusal(uuid4(), EnumCustomerKeyRefusalReason.NO_PROVIDER_KEY_REGISTERED)
    )
    assert terminal.route is None
    assert terminal.provider is None


async def test_a_routing_failure_that_is_not_a_refusal_claims_no_credential() -> None:
    """The negative control: ``none`` is not sprayed over every routing failure."""
    terminal = await _terminal_for(RuntimeError("no tier has a configured endpoint"))
    assert terminal.credential_source is None
