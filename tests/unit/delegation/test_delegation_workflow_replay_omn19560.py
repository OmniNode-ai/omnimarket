# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Recorded inputs must replay without weakening superseded-response rejection."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
from omnibase_core.models.delegation.wire import ModelBudgetLimits
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import decode, encode
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

pytestmark = [
    pytest.mark.unit,
    pytest.mark.usefixtures(
        "stub_provider_quota_reader", "frontier_unconfigured_bifrost"
    ),
]

_CID = UUID("9f77f33a-1956-4000-8000-000000000001")
_CONTENT = "### ANSWER\ndef test_verify_registration():\n    assert True"


def _request() -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type="test",
        correlation_id=_CID,
        emitted_at=datetime(2026, 10, 3, tzinfo=UTC),
    )


def _decision(tier: str = "local") -> ModelRoutingDecision:
    local = tier == "local"
    model = "qwen3-coder-30b" if local else "gemini-2.5-flash"
    return ModelRoutingDecision(
        correlation_id=_CID,
        task_type="test",
        selected_model=model,
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{tier}/{model}"
        ),
        endpoint_url=(
            "http://localhost:8000"
            if local
            else "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        ),
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale=f"Task 'test' routed via tier '{tier}'.",
        tier_name=tier,
        route="local-coder" if local else "cloud-gemini-flash",
        provider="local" if local else "gemini",
    )


def _inference(events: list[BaseModel]) -> ModelInferenceIntent:
    return next(event for event in events if isinstance(event, ModelInferenceIntent))


def _normalised_outputs(events: list[BaseModel]) -> bytes:
    """Normalise the terminal's wall-clock timestamp and measured elapsed times.

    Keep attempt IDs, content, provenance, costs and all other output fields.
    The request timestamp is recorded input and is deliberately preserved.
    """
    rows = []
    for event in events:
        payload = event.model_dump(mode="json")
        if isinstance(event, ModelDelegationResult):
            payload.pop("latency_ms")
            for attempt in payload["escalation_history"]:
                attempt.pop("latency_ms")
                attempt.pop("attempted_at")
        rows.append({"type": type(event).__name__, "payload": payload})
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def captured_four_event_fixture(tmp_path: Path) -> Path:
    """Capture a completed run, including the attempt ID it actually emitted."""
    handler = HandlerDelegationWorkflow(workflows={})
    request, decision = _request(), _decision()
    outputs: list[BaseModel] = [*handler.handle_delegation_request(request)]
    routed = handler.handle_routing_decision(decision)
    outputs.extend(routed)
    intent = _inference(routed)
    assert intent.inference_attempt_id is not None
    response = ModelInferenceResponseData(
        correlation_id=_CID,
        inference_attempt_id=intent.inference_attempt_id,
        content=_CONTENT,
        model_used=intent.model,
        route=intent.route,
        provider=intent.provider,
    )
    outputs.extend(handler.handle_inference_response(response))
    gate = ModelQualityGateResult(
        correlation_id=_CID,
        passed=True,
        quality_score=1.0,
        failure_reasons=(),
        fallback_recommended=False,
    )
    terminal_events = handler.handle_gate_result(gate)
    outputs.extend(terminal_events)
    assert handler.workflows[_CID].state == EnumDelegationState.COMPLETED
    assert any(isinstance(event, ModelDelegationCompleted) for event in terminal_events)

    fixture_path = tmp_path / "completed_delegation.json"
    fixture_path.write_text(
        json.dumps(
            {
                "inputs": [
                    event.model_dump(mode="json")
                    for event in (request, decision, response, gate)
                ],
                "outputs": json.loads(_normalised_outputs(outputs)),
                "terminal": json.loads(_normalised_outputs(terminal_events)),
            }
        )
    )
    return fixture_path


def test_delegation_workflow_replays_captured_four_events_twice(
    captured_four_event_fixture: Path,
) -> None:
    recording = json.loads(captured_four_event_fixture.read_text())
    assert len(recording["inputs"]) == 4
    request = ModelDelegationRequest.model_validate(recording["inputs"][0])
    decision = ModelRoutingDecision.model_validate(recording["inputs"][1])
    response = ModelInferenceResponseData.model_validate(recording["inputs"][2])
    gate = ModelQualityGateResult.model_validate(recording["inputs"][3])

    replay_outputs = []
    for _ in range(2):
        handler = HandlerDelegationWorkflow(workflows={})
        outputs: list[BaseModel] = [*handler.handle_delegation_request(request)]
        routed = handler.handle_routing_decision(decision)
        outputs.extend(routed)
        assert _inference(routed).inference_attempt_id == response.inference_attempt_id
        assert handler.handle_routing_decision(decision) == []
        assert (
            handler.workflows[_CID].current_inference_attempt_id
            == response.inference_attempt_id
        )
        accepted = handler.handle_inference_response(response)
        assert any(isinstance(event, ModelQualityGateIntent) for event in accepted)
        assert handler.workflows[_CID].stale_response_rejections == []
        outputs.extend(accepted)
        terminal_events = handler.handle_gate_result(gate)
        outputs.extend(terminal_events)
        assert handler.workflows[_CID].state == EnumDelegationState.COMPLETED
        assert any(
            isinstance(event, ModelDelegationCompleted) for event in terminal_events
        )
        assert json.loads(_normalised_outputs(terminal_events)) == recording["terminal"]
        normalised = _normalised_outputs(outputs)
        assert json.loads(normalised) == recording["outputs"]
        replay_outputs.append(normalised)

    assert replay_outputs[0] == replay_outputs[1]


def test_delegation_workflow_escalation_rotates_id_and_rejects_superseded_response() -> (
    None
):
    handler = HandlerDelegationWorkflow(workflows={})
    handler.handle_delegation_request(_request())
    first = _inference(handler.handle_routing_decision(_decision()))
    escalation = handler.handle_inference_response(
        ModelInferenceResponseData(
            correlation_id=_CID,
            inference_attempt_id=first.inference_attempt_id,
            content="",
            model_used=first.model,
            latency_ms=50,
            error_message="401 Unauthorized: missing API key",
            route=first.route,
            provider=first.provider,
        )
    )
    assert any(isinstance(event, ModelRoutingIntent) for event in escalation)
    # Continue in a fresh handler after a durable state round-trip. The next
    # ordinal must survive this reload, rather than restarting at the first ID.
    handler = HandlerDelegationWorkflow(
        workflows={_CID: decode(encode(handler.workflows[_CID]))}
    )
    second = _inference(handler.handle_routing_decision(_decision("cheap_cloud")))
    assert first.inference_attempt_id != second.inference_attempt_id
    workflow = handler.workflows[_CID]
    assert (
        handler.handle_inference_response(
            ModelInferenceResponseData(
                correlation_id=_CID,
                inference_attempt_id=first.inference_attempt_id,
                content=_CONTENT,
                model_used=first.model,
                route=first.route,
                provider=first.provider,
            )
        )
        == []
    )
    assert workflow.state == EnumDelegationState.ROUTED
    assert workflow.inference_content is None
    assert workflow.current_inference_attempt_id == second.inference_attempt_id
    assert len(workflow.stale_response_rejections) == 1
    rejection = workflow.stale_response_rejections[0]
    assert rejection.rejected_attempt_id == first.inference_attempt_id
    assert rejection.current_attempt_id == second.inference_attempt_id


def test_delegation_workflow_compliance_repair_id_replays_and_rejects_prior_attempt() -> (
    None
):
    request = ModelDelegationRequest.model_validate(
        {
            **_request().model_dump(),
            "output_schema_key": "review_output",
            "compliance_budget": ModelBudgetLimits(
                max_tokens=100_000, max_cost_usd=100.0, max_time_s=1_000.0
            ),
        }
    )
    repairs = []
    for _ in range(2):
        handler = HandlerDelegationWorkflow(workflows={})
        handler.handle_delegation_request(request)
        first = _inference(handler.handle_routing_decision(_decision()))
        noncompliant = ModelInferenceResponseData(
            correlation_id=_CID,
            inference_attempt_id=first.inference_attempt_id,
            content="not valid JSON",
            model_used=first.model,
            route=first.route,
            provider=first.provider,
        )
        repair_events = handler.handle_inference_response(noncompliant)
        repair = _inference(repair_events)
        assert repair.inference_attempt_id is not None
        assert repair.inference_attempt_id != first.inference_attempt_id
        assert handler.workflows[_CID].state == EnumDelegationState.ROUTED
        assert handler.handle_inference_response(noncompliant) == []
        assert (
            handler.workflows[_CID].current_inference_attempt_id
            == repair.inference_attempt_id
        )
        assert len(handler.workflows[_CID].stale_response_rejections) == 1
        repairs.append(_normalised_outputs(repair_events))
    assert repairs[0] == repairs[1]


def test_delegation_workflow_attempt_ordinal_survives_codec_and_defaults_for_old_rows() -> (
    None
):
    handler = HandlerDelegationWorkflow(workflows={})
    handler.handle_delegation_request(_request())
    intent = _inference(handler.handle_routing_decision(_decision()))
    persisted = encode(handler.workflows[_CID])
    restored = decode(persisted)
    assert restored.inference_attempt_ordinal == 1
    assert restored.current_inference_attempt_id == intent.inference_attempt_id
    old_row = json.loads(persisted)
    del old_row["inference_attempt_ordinal"]
    assert decode(json.dumps(old_row)).inference_attempt_ordinal == 0
