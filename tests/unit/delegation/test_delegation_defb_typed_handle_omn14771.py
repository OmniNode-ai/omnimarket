# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-14771 (S8 PR1): def-B typed entrypoint for HandlerDelegationWorkflow.

The orchestrator handler was converted from an untyped ``handle(payload: object)``
with an internal envelope/dict sniff to the canonical definition-B shape: a typed
``handle(request) -> list[BaseModel]`` that dispatches off ``type(request)``
through the existing ``_PER_STEP_DISPATCH`` table. This suite pins the PR1 DoD:

  * each contract-declared event_model routes to its per-step handler (six at
    OMN-14771; a seventh, the OMN-17397 routing-failure terminal, since);
  * the FSM advances on the typed path (invocation command + lifecycle, the two
    edges the OMN-13477 parity suite does not already drive through ``handle()``);
  * ``correlation_id`` propagates from the inbound typed model on the legacy path;
  * the deleted ``uuid4()`` correlation FABRICATION never resurfaces, and the
    envelope type is absent from the core module (C-core).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.enums import EnumInvocationKind
from omnibase_core.enums.enum_agent_protocol import EnumAgentProtocol
from omnibase_core.enums.enum_agent_task_lifecycle_type import (
    EnumAgentTaskLifecycleType,
)
from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    _PER_STEP_DISPATCH,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_TEST_ENDPOINT_URL = "http://delegation-llm.test:8000"
_HANDLER_PATH = Path(
    "src/omnimarket/nodes/node_delegation_orchestrator/handlers/"
    "handler_delegation_workflow.py"
)


# ---------------------------------------------------------------------------
# Canonical builders — one per contract-declared event_model.
# ---------------------------------------------------------------------------


def _request(cid: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for normalize_status.",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=cid,
        emitted_at=datetime.now(UTC),
    )


def _invocation(cid: UUID) -> ModelInvocationCommand:
    return ModelInvocationCommand(
        task_id=uuid4(),
        correlation_id=cid,
        target_ref="agent://remote",
        invocation_kind=EnumInvocationKind.AGENT,
        agent_protocol=EnumAgentProtocol.A2A,
    )


def _routing_decision(cid: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=cid,
        task_type="test",
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url=_TEST_ENDPOINT_URL,
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are an assistant.",
        rationale="Routing test.",
    )


def _inference_response(cid: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=cid,
        content="def test_x(): pass",
        model_used="qwen3-coder-30b",
        llm_call_id="chatcmpl-omn14771",
        latency_ms=100,
        prompt_tokens=50,
        completion_tokens=20,
        total_tokens=70,
    )


def _gate_result(cid: UUID) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=cid,
        passed=True,
        quality_score=0.9,
        failure_reasons=(),
        fallback_recommended=False,
    )


def _lifecycle(cid: UUID) -> ModelAgentTaskLifecycleEvent:
    return ModelAgentTaskLifecycleEvent(
        task_id=uuid4(),
        correlation_id=cid,
        lifecycle_type=EnumAgentTaskLifecycleType.COMPLETED,
        remote_task_handle="remote-123",
        occurred_at=datetime.now(UTC),
    )


def _routing_failure_terminal(cid: UUID) -> ModelBoundaryFailureTerminal:
    """OMN-17397: the terminal omnibase_infra's consume boundary publishes."""
    return ModelBoundaryFailureTerminal(
        correlation_id=cid,
        failure_class="ProtocolConfigurationError",
        failure_code="ONEX_CORE_041_INVALID_CONFIGURATION",
        retryable=False,
        failure_reason=(
            "ProtocolConfigurationError: [ONEX_CORE_041_INVALID_CONFIGURATION] "
            "No tier has a configured endpoint"
        ),
        origin_topic="onex.cmd.omnibase-infra.delegation-routing-request.v1",  # onex-topic-allow: verbatim from the live incident trace
    )


_ALL_BUILDERS = (
    (_request, "handle_delegation_request"),
    (_invocation, "handle_invocation_command"),
    (_routing_decision, "handle_routing_decision"),
    (_inference_response, "handle_inference_response"),
    (_gate_result, "handle_gate_result"),
    (_lifecycle, "handle_agent_task_lifecycle"),
    (_routing_failure_terminal, "handle_routing_failure_terminal"),
)


# ---------------------------------------------------------------------------
# Routing: each typed model reaches exactly its per-step handler.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTypedDispatchRouting:
    def test_all_contract_models_covered(self) -> None:
        """The builder set matches the dispatch table one-for-one."""
        dispatch_methods = set(_PER_STEP_DISPATCH.values())
        builder_methods = {method for _, method in _ALL_BUILDERS}
        assert builder_methods == dispatch_methods

    @pytest.mark.parametrize(("builder", "method_name"), _ALL_BUILDERS)
    async def test_handle_routes_typed_model_to_its_per_step_handler(
        self, builder: object, method_name: str
    ) -> None:
        """``handle(model)`` invokes precisely the per-step handler for its type."""
        handler = HandlerDelegationWorkflow(workflows={})
        model = builder(uuid4())  # type: ignore[operator]

        spy = MagicMock(return_value=[])
        # Spy every per-step target so a mis-route to a sibling is caught too.
        for target in set(_PER_STEP_DISPATCH.values()):
            setattr(handler, target, MagicMock(return_value=[]))
        setattr(handler, method_name, spy)

        await handler.handle(model)

        spy.assert_called_once_with(model)
        for target in set(_PER_STEP_DISPATCH.values()) - {method_name}:
            getattr(handler, target).assert_not_called()

    async def test_undeclared_payload_type_fails_closed(self) -> None:
        """No catch-all: an unsupported type raises rather than being swallowed."""
        handler = HandlerDelegationWorkflow(workflows={})
        with pytest.raises(ValueError, match="Unsupported delegation workflow"):
            await handler.handle(object())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# FSM advance on the typed path (the two edges not covered by OMN-13477 parity).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTypedPathFsmAdvance:
    async def test_invocation_command_advances_received_to_routed(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        await handler.handle(_request(cid))
        assert handler.workflows[cid].state == EnumDelegationState.RECEIVED

        await handler.handle(_invocation(cid))
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

    async def test_lifecycle_completed_before_progress_terminates_completed(
        self,
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        await handler.handle(_request(cid))
        await handler.handle(_invocation(cid))

        await handler.handle(_lifecycle(cid))
        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED


# ---------------------------------------------------------------------------
# Correlation propagation + no uuid4 fabrication (HOLE-3).
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCorrelationPropagation:
    @pytest.mark.parametrize(("builder", "_method"), _ALL_BUILDERS)
    async def test_handle_async_propagates_inbound_correlation_id(
        self, builder: object, _method: str
    ) -> None:
        """Output correlation is the inbound model's — never a fresh fabrication."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        model = builder(cid)  # type: ignore[operator]

        output = await handler.handle_async(model)

        assert output.correlation_id == cid

    def test_uuid4_correlation_fabrication_is_deleted(self) -> None:
        """The deleted coercion helpers must not reappear (regression guard)."""
        assert not hasattr(HandlerDelegationWorkflow, "_coerce_payload_correlation_id")
        assert not hasattr(HandlerDelegationWorkflow, "_coerce_payload_dict")

    def test_core_module_has_no_event_envelope(self) -> None:
        """C-core (definition B): the envelope type is absent from this handler."""
        source = _HANDLER_PATH.read_text(encoding="utf-8")
        assert "ModelEventEnvelope" not in source
