# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge-unavailable deterministic-floor acceptance on the BUS path (OMN-13959).

Parity with the bus-less local port fix: the bus orchestrator's
``handle_gate_result`` applies the task-class ``required_bar`` to the gate score.
For ``code_generation`` (bar 0.85) a judge outage (429 → ``JUDGE_FAILED``) leaves
the deterministic-only graded score (~0.733) below the bar, because the judge's
0.4 adequacy band is absent. Before OMN-13959 the orchestrator rejected a valid
LOCAL artifact and escalated (or terminated) during a cloud-judge outage.

OMN-13959: when the gate result carries ``score_source=deterministic_acceptance``
for a verifiable class (deterministic floor passed, judge NOT combined) the
orchestrator falls back to the deterministic-floor verdict instead of the
un-meetable combined bar, so a valid local artifact COMPLETES on the local tier.
When the judge IS reachable the score is ``combined`` and the full bar still
applies (proven by the OMN-13642/OMN-13470 suites).

Every hop is the REAL handler; only the judge inference is an injected bridge that
raises (the ``JUDGE_LLM_CALL_FAILED`` path a live z.ai GLM 429 drives).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from pydantic import BaseModel

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_FAILED,
)
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)

# A good-but-mechanically-incomplete code answer: clears the code_generation
# deterministic floor (compiles / final-artifact-only / non-empty) but carries none
# of the convention/regression heuristic markers -> deterministic-only ~0.733,
# below the 0.85 code_generation bar without the judge.
_GOOD_CODE = "def add(a: int, b: int) -> int:\n    return a + b"

# Self-contained bifrost contract: ``code_generation`` routes (tier_order
# [local, ...]) to a local backend declaring the ``code_generation`` capability +
# a COMPLETE verbatim endpoint URL, so routing resolves host-independently (CI has
# no ~/.omninode overlay). The JUDGE backend (cloud-glm) is resolved separately by
# the judge adapter, not from this delegation contract.
_BIFROST_CODEGEN = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: local-coder\n"
    '    endpoint_url: "http://test-codegen:8001/v1/chat/completions"\n'
    '    model_name: "Qwen3.6-35B-A3B"\n'
    "    tier: local\n"
    "    timeout_ms: 30000\n"
    "    max_tokens: 8192\n"
    "    capabilities: [code_generation, reasoning, research]\n"
    "routing_rules:\n"
    '  - rule_id: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"\n'
    "    priority: 10\n"
    "    task_class: code_generation\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [code_generation]\n"
    "    backend_ids: [local-coder]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "ffffffff-ffff-4fff-8fff-ffffffffffff"\n'
    "default_backends:\n"
    "  - local-coder\n"
    "circuit_breaker:\n"
    "  failure_threshold: 5\n"
    "  window_seconds: 30\n"
    "failover:\n"
    "  max_attempts: 3\n"
    "  backoff_base_ms: 500\n"
    "shadow_mode:\n"
    "  enabled: false\n"
    '  policy_version: "test"\n'
    "  log_sample_rate: 1.0\n"
    "  comparison_logging_enabled: true\n"
    "  max_shadow_latency_ms: 5.0\n"
)


class _UnavailableJudgeAdapter(ModelInferenceAdapter):
    """Judge inference bridge whose ``infer`` raises — simulates a live 429/outage.

    ``resolved_model_id`` returns a concrete id so the judge records honest
    provenance; ``infer`` raises, driving ``HandlerJudgeAdequacy.score`` to the
    ``JUDGE_FAILED`` / ``JUDGE_LLM_CALL_FAILED`` verdict (no score) — exactly the
    z.ai GLM 429 path. No network call.
    """

    def __init__(self) -> None:
        self.calls = 0

    def resolved_model_id(self) -> str:
        return "glm-5.2"

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls += 1
        raise RuntimeError(
            "Client error '429 Too Many Requests' for url 'https://api.z.ai/...'"
        )


def _reset_routing_cache() -> None:
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

    _h._config = None
    _h._load_bifrost_endpoints.cache_clear()


def _inference_response(
    intent: ModelInferenceIntent, content: str
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=intent.correlation_id,
        content=content,
        model_used=intent.model,
        llm_call_id="chatcmpl-test",
        latency_ms=1,
        prompt_tokens=10,
        completion_tokens=8,
        total_tokens=18,
    )


def _terminal_topics(terminal_events: list[BaseModel]) -> list[str | None]:
    return [
        (
            TOPIC_ID_DELEGATION_COMPLETED
            if isinstance(e, ModelDelegationCompleted)
            else TOPIC_ID_DELEGATION_FAILED
        )
        for e in terminal_events
        if isinstance(e, ModelDelegationResult)
    ]


@pytest.mark.unit
class TestJudgeUnavailableFloorRealDispatchPath:
    """Drive the FULL chain for ``code_generation`` with an UNAVAILABLE judge.

    The gate hop runs ``handle_async`` over a judge bridge that raises, so the
    verdict is ``JUDGE_FAILED`` (no score) and the published gate result is
    ``score_source=deterministic_acceptance``, ``passed=True``, score ~0.733.
    ``handle_gate_result`` must fall back to the deterministic floor and emit
    ``delegation-completed`` even though the deterministic-only score is below the
    0.85 bar.
    """

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        _reset_routing_cache()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_CODEGEN, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _reset_routing_cache()

    async def _run_full_chain(
        self,
        workflow: HandlerDelegationWorkflow,
        request: ModelDelegationRequest,
    ) -> tuple[ModelQualityGateResult, list[BaseModel], _UnavailableJudgeAdapter]:
        routing_handler = HandlerRoutingIntent()
        bridge = _UnavailableJudgeAdapter()
        gate_handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(inference_bridge=bridge)
        )

        routing_intents = workflow.handle_delegation_request(request)
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        decision = routing_handler.handle(routing_intents[0])
        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        response = _inference_response(inference_intents[0], _GOOD_CODE)
        gate_intents = workflow.handle_inference_response(response)
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        gate_output = await gate_handler.handle_async(gate_intents[0])
        gate_result = next(
            e for e in gate_output.events if isinstance(e, ModelQualityGateResult)
        )
        terminal_events = workflow.handle_gate_result(gate_result)
        return gate_result, terminal_events, bridge

    @pytest.mark.asyncio
    async def test_judge_unavailable_completes_on_local_tier(self) -> None:
        workflow = HandlerDelegationWorkflow(workflows={})
        request = ModelDelegationRequest(
            prompt="Write add(a, b).",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )
        gate_result, terminal_events, bridge = await self._run_full_chain(
            workflow, request
        )

        # The judge was attempted and failed closed -> deterministic-only result.
        assert bridge.calls == 1
        assert gate_result.passed is True
        assert gate_result.score_source == "deterministic_acceptance"
        assert gate_result.quality_score < 0.85
        # OMN-13959: the orchestrator falls back to the deterministic floor and
        # COMPLETES on the local tier instead of escalating during the judge outage.
        assert TOPIC_ID_DELEGATION_COMPLETED in _terminal_topics(terminal_events)
        wf = workflow.workflows[request.correlation_id]
        assert wf.state == EnumDelegationState.COMPLETED
