# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus orchestrator preserves the delegated prompt for deterministic grounding."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelBudgetLimits,
    ModelDelegationRequest,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
)

from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

_REVIEW_RESPONSE_CONTRACT: dict[str, object] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": ["approve", "request_changes", "comment"],
        },
        "summary": {"type": "string"},
        "findings": {"type": "array"},
    },
    "required": ["verdict", "summary", "findings"],
    "additionalProperties": False,
}
_DELEGATED_PROMPT = "Reply with the single word: alive. Nothing else."
# ---------------------------------------------------------------------------
# AC1: the orchestrator stamps grounding_source on BOTH bus-path gate sites
# ---------------------------------------------------------------------------


def _decision(correlation_id: UUID) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type="test",
        selected_model="provider-model",
        selected_backend_id=uuid4(),
        endpoint_url="https://provider.example/v1/chat/completions",
        cost_tier="low",
        max_context_tokens=65_536,
        max_tokens=4_096,
        system_prompt="system",
        rationale="OMN-20340 focused routing decision.",
        tier_name="local",
    )


def _response(correlation_id: UUID, content: str) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content=content,
        model_used="provider-model",
        latency_ms=5,
        prompt_tokens=2,
        completion_tokens=3,
        total_tokens=5,
    )


@pytest.mark.unit
def test_orchestrator_legacy_path_sets_grounding_source() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = ModelDelegationRequest(
        prompt=_DELEGATED_PROMPT,
        task_type="test",
        correlation_id=correlation_id,
        emitted_at=datetime.now(UTC),
    )
    workflow.handle_delegation_request(request)
    workflow.handle_routing_decision(_decision(correlation_id))

    intents = workflow.handle_inference_response(_response(correlation_id, "alive"))

    assert len(intents) == 1
    assert isinstance(intents[0], ModelQualityGateIntent)
    assert intents[0].payload.grounding_source == request.prompt


@pytest.mark.unit
def test_orchestrator_compliance_path_sets_grounding_source() -> None:
    workflow = HandlerDelegationWorkflow(workflows={})
    correlation_id = uuid4()
    request = ModelDelegationRequest.model_validate(
        {
            "prompt": _DELEGATED_PROMPT,
            "task_type": "review",
            "correlation_id": correlation_id,
            "emitted_at": datetime.now(UTC),
            "output_schema_key": "review_output",
            "response_contract": _REVIEW_RESPONSE_CONTRACT,
            "compliance_budget": ModelBudgetLimits(
                max_tokens=100_000, max_cost_usd=100.0, max_time_s=1_000.0
            ),
        }
    )
    workflow.handle_delegation_request(request)
    workflow.handle_routing_decision(_decision(correlation_id))

    intents = workflow.handle_inference_response(
        _response(
            correlation_id,
            '{"verdict": "approve", "summary": "ok", "findings": []}',
        )
    )

    gate_intents = [i for i in intents if isinstance(i, ModelQualityGateIntent)]
    assert len(gate_intents) == 1
    assert gate_intents[0].payload.grounding_source == request.prompt
