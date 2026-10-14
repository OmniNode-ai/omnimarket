# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An untested code answer is refused on the BUS path (OMN-21077).

``code_generation`` is a code class: it is accepted on its deterministic checks
and an executed test run, never on a score. The bus carries no named test run,
so a valid ``code_generation`` artifact is refused as INSUFFICIENT and the
ladder stops on the first rung: no costlier rung can supply the missing tests.

Every hop uses the real handler; acceptance never calls the judge (OMN-20164).
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    EnumQualityScoreComparison,
    ModelInferenceIntent,
    ModelInferenceResponseData,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from pydantic import BaseModel

from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.models.delegation.wire.model_quality_gate import (
    NO_NAMED_TEST_RUN_REASON,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_FAILED,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_mod,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationCompleted,
    ModelDelegationFailed,
    ModelDelegationResult,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

# A good-but-mechanically-incomplete code answer: clears the code_generation
# deterministic floor (compiles / final-artifact-only / non-empty) but carries none
# of the convention/regression heuristic markers -> deterministic-only ~0.733,
# below the declared 0.85 code_generation bar.
_GOOD_CODE = "### ANSWER\ndef add(a: int, b: int) -> int:\n    return a + b"

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
    "    provider: local\n"
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
    """Valid code below the required bar is refused through the bus chain."""

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
        gate_handler = HandlerQualityGateIntent()

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
    async def test_untested_code_answer_is_refused_on_local_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Keep the terminal evidence covered with a single draft and no next tier.
        monkeypatch.setattr(workflow_mod, "is_free_tier", lambda _tier: False)
        monkeypatch.setattr(workflow_mod, "next_eligible_tier", lambda *_a, **_k: None)
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

        # Acceptance never calls the judge (OMN-20164).
        assert bridge.calls == 0
        # OMN-21077: no named test run reached the gate, so the code answer is
        # not checked and is refused, and no rung can cure that.
        assert gate_result.passed is False
        assert gate_result.score_source == "deterministic_acceptance"
        assert NO_NAMED_TEST_RUN_REASON in gate_result.failure_reasons
        assert gate_result.no_rung_can_satisfy is True
        assert TOPIC_ID_DELEGATION_FAILED in _terminal_topics(terminal_events)
        assert TOPIC_ID_DELEGATION_COMPLETED not in _terminal_topics(terminal_events)
        terminal = next(
            event
            for event in terminal_events
            if isinstance(event, ModelDelegationFailed)
        )
        assert terminal.required_quality_bar == 0.85
        assert terminal.score_vs_required_bar is EnumQualityScoreComparison.BELOW_BAR
        assert NO_NAMED_TEST_RUN_REASON in terminal.failed_acceptance_criteria
        wf = workflow.workflows[request.correlation_id]
        assert wf.state == EnumDelegationState.FAILED
