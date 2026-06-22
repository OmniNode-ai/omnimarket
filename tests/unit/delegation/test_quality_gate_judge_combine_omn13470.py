# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13470: LLM-judge adequacy combined into the delegation quality gate.

The deterministic check set for verifiable code classes (compiles /
final_artifact / passes_existing_tests) is a HARD FLOOR for refusals/empties but
is too strict to be the sole adequacy authority — a correct but mechanically-
incomplete answer scores ~0.733 and fails the 0.85 required_bar. This suite pins:

  * a good-but-mechanically-incomplete code answer + a high judge adequacy score
    is COMBINED (score_source="combined") and clears the bar (PASSES);
  * a genuine refusal / inadequate answer still FAILS even with a maximal judge
    score (the deterministic floor hard-blocks before the combine), preserving
    the OMN-13409 refusal-not-pass invariant;
  * the judge call is an EFFECT on the canonical inference bridge (injected fake
    in tests), captured as a durable, replayable ModelDelegationJudgeVerdictEvent;
  * the real dispatch path (orchestrator → quality gate intent → judge effect →
    combine → orchestrator gate result) completes a good code answer and fails a
    refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)

from omnimarket.events.delegation_judge_verdict import (
    EnumDelegationJudgeVerdict,
    ModelDelegationJudgeVerdictEvent,
)
from omnimarket.inference.adapter_inference_bridge import ModelInferenceAdapter
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

_CODE_GEN_DOD = (
    "compiles_without_errors",
    "final_artifact_only",
    "passes_existing_tests",
)
_CODE_GEN_HEUR = (
    "no_refusal",
    "follows_codebase_conventions",
    "no_obvious_regressions",
)

# A good-but-mechanically-incomplete code answer: it compiles, is a single
# artifact, and is non-empty (deterministic floor passes), but it carries none of
# the convention/regression markers, so the deterministic-only graded score is
# ~0.733 and fails the 0.85 bar without the judge.
_GOOD_CODE = "```python\ndef add(a: int, b: int) -> int:\n    return a + b\n```"

_REFUSAL = "I cannot help with that request."


class _FakeBridge(ModelInferenceAdapter):
    """Records the judge call and returns a canned adequacy JSON response."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        self.calls.append(
            {
                "model_key": model_key,
                "user_prompt": user_prompt,
                "temperature": temperature,
            }
        )
        return self._response


class _RaisingBridge(ModelInferenceAdapter):
    """Raises on infer to exercise the judge fail-closed path."""

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        raise RuntimeError("judge endpoint unreachable")


def _code_gate_input(content: str) -> ModelQualityGateInput:
    return ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="code_generation",
        llm_response_content=content,
        dod_deterministic=_CODE_GEN_DOD,
        dod_heuristic=_CODE_GEN_HEUR,
    )


# ---------------------------------------------------------------------------
# Pure-reducer combine unit tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateCombineUnit:
    def test_good_incomplete_code_fails_without_judge(self) -> None:
        """Deterministic-only: a good answer scores below the 0.85 bar."""
        result = quality_gate_delta(_code_gate_input(_GOOD_CODE))
        # The gate itself returns passed=True (deterministic floor cleared) but the
        # graded score is below the required bar, which the orchestrator rejects.
        assert result.quality_score < 0.85
        assert result.score_source != "combined"

    def test_good_incomplete_code_passes_with_high_judge(self) -> None:
        """A high judge adequacy score lifts the combined score over the bar."""
        result = quality_gate_delta(
            _code_gate_input(_GOOD_CODE), judge_adequacy_score=0.95
        )
        assert result.passed is True
        assert result.score_source == "combined"
        assert result.quality_score >= 0.85, (
            f"combined score must clear the bar; got {result.quality_score}"
        )

    def test_low_judge_score_keeps_combined_below_bar(self) -> None:
        """A weak judge score does NOT lift a thin answer over the bar."""
        result = quality_gate_delta(
            _code_gate_input(_GOOD_CODE), judge_adequacy_score=0.30
        )
        assert result.score_source == "combined"
        assert result.quality_score < 0.85

    def test_refusal_blocked_despite_maximal_judge(self) -> None:
        """OMN-13409 invariant: a refusal hard-blocks before the judge combine."""
        result = quality_gate_delta(
            _code_gate_input(_REFUSAL), judge_adequacy_score=0.99
        )
        assert result.passed is False
        assert result.fail_category == "fail_deterministic"
        # The deterministic floor — not a combined score — produced the verdict.
        assert result.score_source != "combined"

    def test_empty_blocked_despite_maximal_judge(self) -> None:
        """An empty answer hard-blocks before the judge combine."""
        result = quality_gate_delta(_code_gate_input(""), judge_adequacy_score=0.99)
        assert result.passed is False
        assert result.fail_category == "fail_deterministic"


# ---------------------------------------------------------------------------
# Judge EFFECT handler tests (canonical inference bridge, captured verdict)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestJudgeAdequacyEffect:
    @pytest.mark.asyncio
    async def test_judge_scores_and_captures_replayable_verdict(self) -> None:
        bridge = _FakeBridge('{"adequacy_score": 0.92, "reasoning": "correct add"}')
        judge = HandlerJudgeAdequacy(inference_bridge=bridge)
        cid = uuid4()
        verdict = await judge.score(
            correlation_id=cid,
            task_type="code_generation",
            prompt="implement add",
            candidate_output=_GOOD_CODE,
            acceptance_criteria=("compiles_without_errors",),
        )
        # The judge call rode the injected bridge (the canonical effect surface).
        assert len(bridge.calls) == 1
        assert isinstance(verdict, ModelDelegationJudgeVerdictEvent)
        assert verdict.actual_score == 0.92
        assert verdict.verdict is EnumDelegationJudgeVerdict.PASS
        # Replay identity: the recorded event hash recomputes deterministically.
        assert verdict.event_hash == verdict.compute_event_hash()

    @pytest.mark.asyncio
    async def test_judge_failure_is_failclosed_not_zero(self) -> None:
        judge = HandlerJudgeAdequacy(inference_bridge=_RaisingBridge())
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="code_generation",
            prompt="implement add",
            candidate_output=_GOOD_CODE,
        )
        assert verdict.verdict is EnumDelegationJudgeVerdict.JUDGE_FAILED
        assert verdict.actual_score is None
        assert verdict.failure_kind == "JUDGE_LLM_CALL_FAILED"

    @pytest.mark.asyncio
    async def test_unparseable_judge_response_failclosed(self) -> None:
        judge = HandlerJudgeAdequacy(inference_bridge=_FakeBridge("not json at all"))
        verdict = await judge.score(
            correlation_id=uuid4(),
            task_type="code_generation",
            prompt="implement add",
            candidate_output=_GOOD_CODE,
        )
        assert verdict.verdict is EnumDelegationJudgeVerdict.JUDGE_FAILED
        assert verdict.failure_kind == "JUDGE_PARSE_FAILED"


# ---------------------------------------------------------------------------
# Quality-gate-intent handler: combine + emit verdict event
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestQualityGateIntentJudgeWiring:
    @pytest.mark.asyncio
    async def test_intent_combines_judge_and_emits_verdict(self) -> None:
        bridge = _FakeBridge('{"adequacy_score": 0.95, "reasoning": "adequate"}')
        handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(inference_bridge=bridge)
        )
        intent = ModelQualityGateIntent(payload=_code_gate_input(_GOOD_CODE))
        output = await handler.handle_async(intent)

        gate_results = [
            e for e in output.events if type(e).__name__ == "ModelQualityGateResult"
        ]
        verdicts = [
            e for e in output.events if isinstance(e, ModelDelegationJudgeVerdictEvent)
        ]
        assert len(gate_results) == 1
        assert len(verdicts) == 1, "judge verdict event must be emitted for projection"
        assert gate_results[0].score_source == "combined"
        assert gate_results[0].quality_score >= 0.85

    @pytest.mark.asyncio
    async def test_intent_refusal_blocked_and_verdict_emitted(self) -> None:
        bridge = _FakeBridge('{"adequacy_score": 0.99, "reasoning": "looks fine"}')
        handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(inference_bridge=bridge)
        )
        intent = ModelQualityGateIntent(payload=_code_gate_input(_REFUSAL))
        output = await handler.handle_async(intent)

        gate_results = [
            e for e in output.events if type(e).__name__ == "ModelQualityGateResult"
        ]
        assert gate_results[0].passed is False
        assert gate_results[0].fail_category == "fail_deterministic"


# ---------------------------------------------------------------------------
# Real-dispatch-path end-to-end (orchestrator → gate intent → judge → combine)
# ---------------------------------------------------------------------------

_BIFROST_CODE = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: cloud-gemini-flash\n"
    '    endpoint_url: "http://test-coder:8000/v1/chat/completions"\n'
    '    model_name: "gemini-2.5-flash-lite"\n'
    "    tier: cheap_cloud\n"
    "    timeout_ms: 30000\n"
    "    capabilities: [code_generation, simple_tasks, document, summarization]\n"
    "routing_rules:\n"
    '  - rule_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"\n'
    "    priority: 10\n"
    "    task_class: code_generation\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [code_generation]\n"
    "    backend_ids: [cloud-gemini-flash]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"\n'
    "default_backends:\n"
    "  - cloud-gemini-flash\n"
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


def _httpx_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
    }
    response.raise_for_status.return_value = None
    return response


@pytest.mark.unit
class TestJudgeCombineRealDispatchPath:
    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_CODE, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()

    def _code_request(self) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Implement an add(a, b) function.",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )

    async def _run_chain(
        self,
        request: ModelDelegationRequest,
        llm_content: str,
        judge_response: str,
    ) -> tuple[object, list[object], HandlerDelegationWorkflow]:
        workflow = HandlerDelegationWorkflow(workflows={})
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(inference_bridge=_FakeBridge(judge_response))
        )

        routing_intents = workflow.handle_delegation_request(request)
        assert isinstance(routing_intents[0], ModelRoutingIntent)
        decision = routing_handler.handle(routing_intents[0])

        inference_intents = workflow.handle_routing_decision(decision)
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response(llm_content)
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])

        gate_intents = workflow.handle_inference_response(response)
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        # Hop: quality gate intent runs the judge EFFECT + combines.
        gate_output = await gate_handler.handle_async(gate_intents[0])
        gate_result = next(
            e
            for e in gate_output.events
            if type(e).__name__ == "ModelQualityGateResult"
        )

        terminal_events = workflow.handle_gate_result(gate_result)
        return gate_result, terminal_events, workflow

    @pytest.mark.asyncio
    async def test_good_code_completes_via_combined_score(self) -> None:
        request = self._code_request()
        gate_result, terminal_events, workflow = await self._run_chain(
            request,
            _GOOD_CODE,
            '{"adequacy_score": 0.95, "reasoning": "correct implementation"}',
        )
        assert gate_result.score_source == "combined"  # type: ignore[attr-defined]
        topics = [
            getattr(e, "topic", None)
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
        ]
        assert TOPIC_ID_DELEGATION_COMPLETED in topics, (
            f"good code answer must complete via combined score; topics={topics}"
        )
        assert (
            workflow.workflows[request.correlation_id].state
            == EnumDelegationState.COMPLETED
        )

    @pytest.mark.asyncio
    async def test_refusal_not_completed_even_with_high_judge(self) -> None:
        request = self._code_request()
        gate_result, terminal_events, workflow = await self._run_chain(
            request,
            "I cannot complete this task.",
            '{"adequacy_score": 0.99, "reasoning": "fine"}',
        )
        assert gate_result.passed is False  # type: ignore[attr-defined]
        topics = [
            getattr(e, "topic", None)
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
        ]
        assert TOPIC_ID_DELEGATION_COMPLETED not in topics, (
            f"refusal must NOT complete even with a high judge score; topics={topics}"
        )
        assert workflow.workflows[request.correlation_id].state in {
            EnumDelegationState.FAILED,
            EnumDelegationState.ROUTED,
        }
