# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real golden and error chains through the delegation orchestrator."""

from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.enums.enum_delegation_routing_disposition import (
    EnumDelegationRoutingDisposition,
)
from omnibase_core.enums.enum_delegation_terminal_outcome import (
    EnumDelegationTerminalOutcome,
)
from omnibase_core.enums.enum_delegation_unrouted_reason import (
    EnumDelegationUnroutedReason,
)
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_COMPLETED_V2,
    TOPIC_ID_DELEGATION_FAILED,
    TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2,
    TOPIC_ID_INFERENCE_REQUEST,
    TOPIC_ID_QUALITY_GATE_REQUEST,
    TOPIC_ID_ROUTING_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
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
from tests.chains.chain_assert import (
    ChainRecorder,
    assert_chain,
    assert_error_chain,
    chain_obligation,
)
from tests.chains.delegation.obligations import load_obligations

pytestmark = pytest.mark.unit

_BACKEND_REF = "local-qwen3-coder-30b"
_ENDPOINT = "http://192.168.86.201:8000"  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for a local LLM endpoint"
_MODEL = "qwen3-coder-30b"
_TENANT = "omn19710-tenant"

_CONTRACT_TOPIC_BY_EVENT_TYPE = {
    "ModelRoutingIntent": TOPIC_ID_ROUTING_REQUEST,
    "ModelInferenceIntent": TOPIC_ID_INFERENCE_REQUEST,
    "ModelQualityGateIntent": TOPIC_ID_QUALITY_GATE_REQUEST,
    "ModelDelegationCompleted": TOPIC_ID_DELEGATION_COMPLETED,
    "ModelDelegationTerminalCompletedV2": TOPIC_ID_DELEGATION_COMPLETED_V2,
    "ModelDelegationFailed": TOPIC_ID_DELEGATION_FAILED,
    "ModelDelegationTerminalFailedUnroutedV2": (TOPIC_ID_DELEGATION_FAILED_UNROUTED_V2),
}


def _make_request(correlation_id: UUID) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",  # type: ignore[arg-type]
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
        tenant_id=_TENANT,
    )


def _make_routing_decision(correlation_id: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model=_MODEL,
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/local-qwen"),
        endpoint_url=_ENDPOINT,
        cost_tier="low",
        tier_name="free_local",
        max_context_tokens=65536,
        max_tokens=4096,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed via tier 'free_local'.",
        selected_backend_ref=_BACKEND_REF,
    )


def _make_success_response(correlation_id: UUID) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content="def test_verify_registration():\n    assert True",
        model_used=_MODEL,
        llm_call_id="chatcmpl-omn19710",
        latency_ms=1200,
        prompt_tokens=1234,
        completion_tokens=567,
        total_tokens=1801,
    )


def _topic_for(event: BaseModel) -> str:
    """Resolve the contract topic, falling back explicitly for unknown test events."""
    # Every current fan-out class is contract-resolved above. The class-name
    # fallback keeps the recorder usable if a new diagnostic event is added
    # before contract_topics exposes a dedicated binding for it.
    return _CONTRACT_TOPIC_BY_EVENT_TYPE.get(type(event).__name__, type(event).__name__)


async def _publish_batch(
    recorder: ChainRecorder,
    events: Sequence[BaseModel],
    correlation_id: UUID,
) -> None:
    for event in events:
        await recorder.publish(
            _topic_for(event),
            event,
            correlation_id=correlation_id,
            causation_id=correlation_id if recorder.events else None,
        )


@chain_obligation(
    "golden:invocation_command_accepted>inference_response_received>"
    "gate_result_received>gate_passed"
)
async def test_real_golden_delegation_orchestrator_chain() -> None:
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow()
    recorder = ChainRecorder(
        EventBusInmemory(environment="test", group="omn19710-golden")
    )

    await _publish_batch(
        recorder,
        handler.handle_delegation_request(_make_request(correlation_id)),
        correlation_id,
    )
    await _publish_batch(
        recorder,
        handler.handle_routing_decision(_make_routing_decision(correlation_id)),
        correlation_id,
    )
    await _publish_batch(
        recorder,
        handler.handle_inference_response(_make_success_response(correlation_id)),
        correlation_id,
    )
    await _publish_batch(
        recorder,
        handler.handle_gate_result(
            ModelQualityGateResult(
                correlation_id=correlation_id,
                passed=True,
                quality_score=0.95,
                failure_reasons=(),
                fallback_recommended=False,
            )
        ),
        correlation_id,
    )

    assert_chain(
        recorder.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelInferenceIntent",
            "ModelQualityGateIntent",
            "ModelDelegationCompleted",
            "ModelDelegationTerminalCompletedV2",
        ],
        terminal_fields={
            "terminal_outcome": EnumDelegationTerminalOutcome.COMPLETED,
            "routing_disposition": EnumDelegationRoutingDisposition.ROUTED,
        },
        correlation_id=correlation_id,
        bus_history_count=await recorder.bus_history_count(),
    )


@chain_obligation("error:routing_boundary_terminalized")
async def test_real_routing_boundary_error_chain() -> None:
    correlation_id = uuid4()
    handler = HandlerDelegationWorkflow()
    recorder = ChainRecorder(
        EventBusInmemory(environment="test", group="omn19710-error")
    )

    await _publish_batch(
        recorder,
        handler.handle_delegation_request(_make_request(correlation_id)),
        correlation_id,
    )
    await _publish_batch(
        recorder,
        handler.handle_boundary_failure_terminal(
            ModelBoundaryFailureTerminal(
                correlation_id=correlation_id,
                origin_topic=TOPIC_ID_ROUTING_REQUEST,
                failure_class="ProtocolConfigurationError",
                failure_code="ONEX_CORE_041_INVALID_CONFIGURATION",
                failure_reason="routing tier configuration did not resolve",
                retryable=False,
            )
        ),
        correlation_id,
    )

    assert_error_chain(
        recorder.events,
        expected_event_types=[
            "ModelRoutingIntent",
            "ModelDelegationFailed",
            "ModelDelegationTerminalFailedUnroutedV2",
        ],
        terminal_fields={
            "terminal_outcome": EnumDelegationTerminalOutcome.FAILED,
            "unrouted_reason": (
                EnumDelegationUnroutedReason.ROUTING_CONFIGURATION_INVALID
            ),
        },
        correlation_id=correlation_id,
        bus_history_count=await recorder.bus_history_count(),
    )


def test_every_chain_obligation_marker_names_a_walker_path() -> None:
    obligation_ids = {obligation.path_id for obligation in load_obligations()}
    used_ids: set[str] = set()
    for path in Path(__file__).parent.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "chain_obligation"
                and node.args
            ):
                marker_id = ast.literal_eval(node.args[0])
                assert isinstance(marker_id, str)
                used_ids.add(marker_id)

    assert used_ids
    assert used_ids <= obligation_ids
