# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The adequacy judge sees the delegated prompt, not the generic template (OMN-20340).

Before this change both gate paths handed the judge the template
``"Judge whether the candidate adequately fulfills a {task_type} task ..."`` as
the requested task, so a candidate that answered the real prompt correctly
("Reply with the single word: alive" -> ``alive``) was scored 0.0-0.1 and the
combined score fell under the 0.800 bar. The bus-path orchestrator now stamps
``grounding_source`` (the delegated prompt) on the quality-gate input, the
intent handler passes it to the judge, and the local port passes its ``prompt``.

The judge inference call is the only injected double (a recording bridge).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelBudgetLimits,
    ModelDelegationRequest,
    ModelInferenceResponseData,
    ModelQualityGateInput,
    ModelQualityGateIntent,
)

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    port_local_delegation_dispatch as port_mod,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_local_delegation_dispatch import (
    LocalDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_llm_delegation_call_effect import (
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
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
_TEMPLATE_FRAGMENT = "Judge whether the candidate adequately fulfills a test task"


class _RecordingJudgeBridge(ModelInferenceAdapter):
    """Judge bridge that records every user prompt and returns a passing score."""

    def __init__(self, *, fail: bool = False) -> None:
        self.user_prompts: list[str] = []
        self._fail = fail

    def resolved_model_id(self) -> str:
        return "judge-model"

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.user_prompts.append(user_prompt)
        if self._fail:
            raise RuntimeError("judge endpoint unreachable")
        return '{"adequacy_score": 1.0, "reasoning": "recorded"}'


def _gate_input(*, grounding_source: str | None) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="test",
        llm_response_content="alive",
        grounding_source=grounding_source,
    )


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


# ---------------------------------------------------------------------------
# AC2: the judge user prompt carries the delegated prompt
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_judge_receives_delegated_prompt_bus_handler() -> None:
    bridge = _RecordingJudgeBridge()
    handler = HandlerQualityGateIntent(
        judge=HandlerJudgeAdequacy(inference_bridge=bridge)
    )

    await handler.handle_async(
        ModelQualityGateIntent(payload=_gate_input(grounding_source=_DELEGATED_PROMPT))
    )

    assert len(bridge.user_prompts) == 1
    assert "Reply with the single word: alive" in bridge.user_prompts[0]
    assert _TEMPLATE_FRAGMENT not in bridge.user_prompts[0]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_judge_receives_template_when_grounding_source_absent() -> None:
    bridge = _RecordingJudgeBridge()
    handler = HandlerQualityGateIntent(
        judge=HandlerJudgeAdequacy(inference_bridge=bridge)
    )

    await handler.handle_async(
        ModelQualityGateIntent(payload=_gate_input(grounding_source=None))
    )

    assert len(bridge.user_prompts) == 1
    assert _TEMPLATE_FRAGMENT in bridge.user_prompts[0]


_LOCAL_BACKEND = ModelResolvedDelegationBackend(
    backend_id="local-coder",
    model_id="Qwen3.6-35B-A3B",
    endpoint_ref="https://local.example/v1/chat/completions",
    tier="local",
    max_tokens=4096,
    timeout_ms=30000,
)


def _effect_returning(content: str) -> port_mod._EffectHandler:
    def _effect(
        request: ModelLlmDelegationCallRequest,
    ) -> ModelLlmDelegationCallResult:
        return ModelLlmDelegationCallResult(
            request_id=request.request_id,
            success=True,
            content=content,
            tokens_in=10,
            tokens_out=20,
            latency_ms=5,
            actual_cost_usd=Decimal("0"),
            savings_usd=Decimal("0"),
        )

    return _effect


@pytest.mark.unit
def test_judge_receives_delegated_prompt_local_dispatch_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        port_mod, "resolve_delegation_backend", lambda *_a, **_k: _LOCAL_BACKEND
    )
    monkeypatch.setattr(port_mod, "next_eligible_tier", lambda *_a, **_k: None)
    monkeypatch.setattr(port_mod, "tier_for_backend", lambda _backend_id: "local")
    monkeypatch.setattr(
        port_mod, "resolve_task_class_max_escalations", lambda _task_type: 0
    )
    bridge = _RecordingJudgeBridge()
    port = LocalDelegationDispatchPort(
        effect_handler=_effect_returning("alive"),
        evidence_db_path=tmp_path / "d.sqlite",
        effect_process_boundary=False,
        judge=HandlerJudgeAdequacy(inference_bridge=bridge),
    )

    asyncio.run(
        port.dispatch(
            prompt=_DELEGATED_PROMPT,
            task_type="test",
            correlation_id=uuid4(),
            max_tokens=256,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=240,
            terminal_delivery_margin_seconds=60,
            quality_contract_mode="extend_task_class",
            acceptance_criteria=(),
            tenant_id=None,
        )
    )

    assert len(bridge.user_prompts) >= 1
    assert all("Reply with the single word: alive" in p for p in bridge.user_prompts)
    assert all(_TEMPLATE_FRAGMENT not in p for p in bridge.user_prompts)


# ---------------------------------------------------------------------------
# AC3: a judge call error yields no numeric score, and the decision line says so
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_judge_unreachable_decision_line_names_call_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bridge = _RecordingJudgeBridge(fail=True)
    handler = HandlerQualityGateIntent(
        judge=HandlerJudgeAdequacy(inference_bridge=bridge)
    )

    with caplog.at_level(logging.INFO):
        output = await handler.handle_async(
            ModelQualityGateIntent(
                payload=_gate_input(grounding_source=_DELEGATED_PROMPT)
            )
        )

    assert "judge_score=None" in caplog.text
    assert "judge_status=JUDGE_LLM_CALL_FAILED" in caplog.text
    verdicts = [e for e in output.events if hasattr(e, "judge_model")]
    assert all(getattr(v, "actual_score", None) is None for v in verdicts)
