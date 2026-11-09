# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23329: the bus workflow does not redraft a missed lead rung on itself.

A free tier's quality-gate miss is re-routed to the same tier (best-of-N). When
the answer that missed came from a lead rung (the small local model placed first
in its tier), the re-route excludes that backend, so the tier's own rung answers
the retry. The control names no lead rung: the re-route excludes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as hw,
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
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures("stub_provider_quota_reader"),
]

_LEAD = "local-small"
_TASK = "summarization"


@pytest.fixture
def _free_local(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hw, "is_free_tier", lambda tier: tier == "local")
    monkeypatch.setattr(hw, "tier_max_retries", lambda _tier: 2)
    monkeypatch.setattr(hw, "next_eligible_tier", lambda *_a, **_k: None)


def _drive_to_gate(handler: HandlerDelegationWorkflow, cid: UUID) -> None:
    handler.handle_delegation_request(
        ModelDelegationRequest.model_validate(
            {
                "prompt": "Summarize: the build passed.",
                "task_type": _TASK,
                "correlation_id": cid,
                "emitted_at": datetime.now(UTC),
            }
        )
    )
    handler.handle_routing_decision(
        ModelRoutingDecision(
            correlation_id=cid,
            task_type=_TASK,
            selected_model="small-local-model-q4",
            selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/lead"),
            selected_backend_ref=_LEAD,
            endpoint_url="http://198.51.100.30:8110/v1/chat/completions",
            cost_tier="low",
            max_context_tokens=12000,
            max_tokens=512,
            system_prompt="You summarize.",
            rationale="Task routed via tier 'local'.",
            tier_name="local",
        )
    )
    handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=cid,
            content="### ANSWER\nok",
            model_used="small-local-model-q4",
            latency_ms=42,
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
        )
    )
    assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED


def _miss(handler: HandlerDelegationWorkflow, cid: UUID) -> ModelRoutingIntent:
    intents = handler.handle_gate_result(
        ModelQualityGateResult(
            correlation_id=cid,
            passed=False,
            quality_score=0.4,
            failure_reasons=("score_below_required_bar",),
            fallback_recommended=True,
        )
    )
    routing = [i for i in intents if isinstance(i, ModelRoutingIntent)]
    assert len(routing) == 1
    return routing[0]


@pytest.mark.usefixtures("_free_local")
def test_local_rung_route_a_lead_miss_is_rerouted_without_the_lead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(hw, "lead_backend_refs", lambda: frozenset({_LEAD}))
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    _drive_to_gate(handler, cid)
    intent = _miss(handler, cid)
    assert intent.min_tier_name == "local"
    assert _LEAD in intent.excluded_backend_refs


@pytest.mark.usefixtures("_free_local")
def test_local_rung_route_control_unnamed_lead_is_not_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failing control: no lead rung named, the retry excludes nothing."""
    monkeypatch.setattr(hw, "lead_backend_refs", lambda: frozenset())
    handler = HandlerDelegationWorkflow(workflows={})
    cid = uuid4()
    _drive_to_gate(handler, cid)
    intent = _miss(handler, cid)
    assert intent.min_tier_name == "local"
    assert _LEAD not in intent.excluded_backend_refs
