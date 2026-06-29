# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13479: refusals/empties must NOT pass the gate on code_generation + research.

OMN-13409 proved the refusal-not-pass invariant on the summarization/document
path only (test_refusal_not_pass_omn13409.py). With the OMN-13470 judge-adequacy
combine landed (a judge score is combined with the deterministic graded score for
verifiable classes), a high judge score could in principle mask a refusal/empty on
the harder classes. This suite extends the invariant to the two task classes the
ticket names — ``code_generation`` and ``research`` — driving the FULL real
dispatch chain (orchestrator FSM -> routing reducer -> inference effect -> quality
gate -> terminal), NOT a handler-isolation shortcut.

The invariant under test, per class:

  * code_generation (a judge-combinable, verifiable class): a genuine refusal or
    an empty/inadequate answer fails the deterministic HARD FLOOR
    (``compiles_without_errors`` / ``passes_existing_tests`` -> ``response_non_empty``)
    and terminates as ``quality_gate_passed=false`` with ``fail_category=
    "fail_deterministic"`` — EVEN WITH the OMN-13470 judge combine active and the
    judge returning a maximal adequacy score (a recorded-from-real GLM verdict of
    0.9). The judge cannot lift a refusal/empty over the bar; the deterministic
    floor blocks before any combine. ``delegation-completed`` is never emitted.

  * research (a prose, non-judge-combinable class): a refusal fails the
    ``no_refusal`` heuristic (``fail_category="fail_heuristic"``) and an empty
    answer fails the ``response_non_empty`` deterministic check
    (``fail_category="fail_deterministic"``); both terminate as
    ``quality_gate_passed=false`` and never emit ``delegation-completed``.

The chain is exercised over the REAL handlers (HandlerRoutingIntent,
HandlerInferenceIntent, HandlerQualityGateIntent, HandlerDelegationWorkflow); only
the outbound httpx provider call is patched to inject a deterministic candidate
body (same substitution boundary the OMN-13409 suite uses), and the judge inference
rides the OMN-13470 RecordedJudgeReplayAdapter (a response captured from a real
z.ai GLM call, pinned to the concrete model id). No handler is mocked.
"""

from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
    ModelRoutingIntent,
)
from pydantic import BaseModel

from omnimarket.events.delegation_judge_verdict import (
    ModelDelegationJudgeVerdictEvent,
)
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
    TOPIC_QUALITY_GATE_RESULT,
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    TOPIC_ROUTING_DECISION,
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    TOPIC_INFERENCE_RESPONSE,
    HandlerInferenceIntent,
)
from tests.fixtures.judge_inference import RecordedJudgeReplayAdapter

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# Refusal / empty / inadequate candidate outputs the gate must reject on BOTH
# classes. The phrase refusals match _REFUSAL_PHRASES; the ultra-short ones match
# the OMN-13409 content-free pre-pass; the empty string fails response_non_empty.
_REFUSAL_PHRASE = "I cannot help with that request."
_REFUSAL_SOFT = "Unable to complete this task."
_REFUSAL_SHORT = "No."
_EMPTY = ""

# An inadequate-but-non-empty candidate exercised on the DISPATCH PATH. A truly
# empty body ("") is rejected UPSTREAM by the inference effect
# (InferenceUsageError "empty message content") before it can reach the gate — a
# distinct, also-correct non-pass termination path, but NOT the gate's adequacy
# verdict. To exercise the GATE's rejection of an inadequate answer end-to-end the
# inference effect must accept the body, so the dispatch-path "inadequate" case is
# a non-empty answer the gate still rejects:
#   * code_generation: prose, not a compilable artifact -> fails
#     compiles_without_errors (deterministic hard floor).
#   * research: a thin, unsupported answer with no sources / no reasoning
#     structure -> fails cites_sources / methodical_analysis (heuristic).
# The gate's rejection of a literally-empty body is pinned directly in the
# unit-level suites above (which drive delta() without the upstream guard).
_INADEQUATE_CODE = "Sorry, here is a description instead of code."
_INADEQUATE_RESEARCH = "It works fine."

# code_generation routes (per task_class_contracts.v1.yaml) to the cheap_cloud
# tier first (tier_order [cheap_cloud, local, claude]) on model glm-5.2 / backend
# cloud-glm. The self-contained bifrost contract below declares exactly that
# backend with a COMPLETE verbatim endpoint URL and NO api_key_ref, so routing
# resolves host-independently (CI has no ~/.omninode overlay and no secret store).
_BIFROST_CODE_GENERATION = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: cloud-glm\n"
    '    endpoint_url: "http://test-codegen:8000/v1/chat/completions"\n'
    '    model_name: "glm-5.2"\n'
    "    tier: cheap_cloud\n"
    "    timeout_ms: 30000\n"
    "    max_tokens: 8192\n"
    "    capabilities: [code_generation, code_review, reasoning, research, test]\n"
    "routing_rules:\n"
    '  - rule_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"\n'
    "    priority: 10\n"
    "    task_class: code_generation\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [code_generation]\n"
    "    backend_ids: [cloud-glm]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"\n'
    "default_backends:\n"
    "  - cloud-glm\n"
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

# research routes (tier_order [local, cheap_cloud, claude]) to the local tier
# first on model Qwen3.6-35B-A3B / backend local-coder. The self-contained
# contract declares that backend (no api_key_ref) so routing resolves the local
# tier without LAN reachability — the inference HTTP call is patched anyway.
_BIFROST_RESEARCH = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: local-coder\n"
    '    endpoint_url: "http://test-research:8000/v1/chat/completions"\n'
    '    model_name: "Qwen3.6-35B-A3B"\n'
    "    tier: local\n"
    "    timeout_ms: 30000\n"
    "    max_tokens: 8192\n"
    "    capabilities: [research, code_generation, code_review, refactor, test]\n"
    "routing_rules:\n"
    '  - rule_id: "cccccccc-cccc-4ccc-8ccc-cccccccccccc"\n'
    "    priority: 10\n"
    "    task_class: research\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [research]\n"
    "    backend_ids: [local-coder]\n"
    "    fallback_policy:\n"
    "      action: escalate_to_next_tier\n"
    "      max_retries: 1\n"
    "      on_exhaust: return_error\n"
    '    shadow_policy_id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd"\n'
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


def _task_class_dod(task_class: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text())
    dod = contract["task_classes"][task_class]["definition_of_done"]
    return (
        tuple(dod.get("deterministic", ())),
        tuple(dod.get("heuristic", ())),
    )


class _CapturingPublisher:
    """Records (topic, payload) pairs published by each worker handler."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, topic: str, payload: object) -> None:
        self.published.append((topic, payload))

    def topics(self) -> list[str]:
        return [t for t, _ in self.published]


def _httpx_response(content: str) -> MagicMock:
    response = MagicMock()
    response.json.return_value = {
        "id": "chatcmpl-test",
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
        },
    }
    response.raise_for_status.return_value = None
    return response


def _terminal_topics(terminal_events: list[BaseModel]) -> list[str | None]:
    return [
        getattr(e, "topic", None)
        for e in terminal_events
        if isinstance(e, ModelDelegationEvent)
    ]


def _reset_routing_cache() -> None:
    """Reset the routing reducer's module-level config + endpoint cache.

    Imported directly (not via a handle) so the module attributes type-check; the
    routing reducer caches the parsed config and bifrost endpoints at module level,
    so a per-test contract swap must clear both at setup and teardown.
    """
    import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

    _h._config = None
    _h._load_bifrost_endpoints.cache_clear()


def _bifrost_fixture(contract_text: str, tmp_path: Path) -> Path:
    """Write a self-contained bifrost contract and reset the routing cache.

    Returns the contract path so the caller can install the env var; teardown
    re-clears the cache via ``_reset_routing_cache``.
    """
    _reset_routing_cache()
    contract_path = tmp_path / "bifrost_delegation.yaml"
    contract_path.write_text(contract_text, encoding="utf-8")
    return contract_path


# ---------------------------------------------------------------------------
# Unit-level quality-gate tests (no orchestrator) — pin the per-class verdict.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCodeGenerationRefusalUnit:
    """The code_generation DoD hard-floor rejects refusals/empties.

    The deterministic checks (compiles_without_errors / final_artifact_only /
    passes_existing_tests->response_non_empty) are a HARD FLOOR (OMN-13470): a
    refusal does not parse as Python and an empty answer is non-empty-failing, so
    both produce fail_category="fail_deterministic" BEFORE any judge combine. A
    maximal judge score cannot lift them — proven in the dispatch-path suite below.
    """

    def _dod_inputs(
        self, content: str, *, judge_adequacy_score: float | None = None
    ) -> ModelQualityGateResult:
        det, heur = _task_class_dod("code_generation")
        return quality_gate_delta(
            ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="code_generation",
                llm_response_content=content,
                dod_deterministic=det,
                dod_heuristic=heur,
            ),
            judge_adequacy_score=judge_adequacy_score,
        )

    @pytest.mark.parametrize(
        "content", [_REFUSAL_PHRASE, _REFUSAL_SOFT, _REFUSAL_SHORT, _EMPTY]
    )
    def test_refusal_or_empty_fails_deterministic_floor(self, content: str) -> None:
        result = self._dod_inputs(content)
        assert result.passed is False, (
            f"code_generation refusal/empty {content!r} must not pass; "
            f"score={result.quality_score}, reasons={result.failure_reasons}"
        )
        assert result.fail_category == "fail_deterministic", (
            f"refusal/empty must hard-block on the deterministic floor; "
            f"got fail_category={result.fail_category}, reasons={result.failure_reasons}"
        )

    @pytest.mark.parametrize(
        "content", [_REFUSAL_PHRASE, _REFUSAL_SOFT, _REFUSAL_SHORT, _EMPTY]
    )
    def test_maximal_judge_cannot_lift_refusal_or_empty(self, content: str) -> None:
        """OMN-13479 core: the OMN-13470 judge combine cannot mask a refusal/empty.

        Even with the maximal judge adequacy score (0.99), the deterministic floor
        blocks first, so the verdict stays fail_deterministic and score_source is
        NOT "combined" (the combine never runs past the floor).
        """
        result = self._dod_inputs(content, judge_adequacy_score=0.99)
        assert result.passed is False
        assert result.fail_category == "fail_deterministic"
        assert result.score_source != "combined", (
            "a refusal/empty must hard-block before the judge combine; "
            f"got score_source={result.score_source}"
        )


@pytest.mark.unit
class TestResearchRefusalUnit:
    """The research DoD rejects refusals (heuristic) and empties (deterministic)."""

    def _dod_inputs(self, content: str) -> ModelQualityGateResult:
        det, heur = _task_class_dod("research")
        return quality_gate_delta(
            ModelQualityGateInput(
                correlation_id=uuid4(),
                task_type="research",
                llm_response_content=content,
                dod_deterministic=det,
                dod_heuristic=heur,
            )
        )

    @pytest.mark.parametrize(
        "content", [_REFUSAL_PHRASE, _REFUSAL_SOFT, _REFUSAL_SHORT]
    )
    def test_refusal_fails_heuristic(self, content: str) -> None:
        result = self._dod_inputs(content)
        assert result.passed is False, (
            f"research refusal {content!r} must not pass; "
            f"score={result.quality_score}, reasons={result.failure_reasons}"
        )
        assert result.fail_category != "pass"
        assert any("REFUSAL" in r for r in result.failure_reasons), (
            f"research refusal must carry a REFUSAL reason; got {result.failure_reasons}"
        )

    def test_empty_fails_deterministic(self) -> None:
        result = self._dod_inputs(_EMPTY)
        assert result.passed is False
        assert result.fail_category == "fail_deterministic", (
            f"empty research answer must hard-block on response_non_empty; "
            f"got fail_category={result.fail_category}, reasons={result.failure_reasons}"
        )


# ---------------------------------------------------------------------------
# Real-dispatch-path end-to-end tests (orchestrator -> routing -> inference ->
# quality gate -> terminal), OMN-13470 judge combine ACTIVE for code_generation.
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCodeGenerationRefusalRealDispatchPath:
    """Drive the full orchestrator -> quality gate dispatch path for code_generation.

    Each handler is the REAL handler; only the outbound httpx call is patched (same
    boundary as the OMN-13409 suite). The quality-gate hop runs ``handle_async``
    with a RecordedJudgeReplayAdapter so the OMN-13470 judge combine is ACTIVE and
    the judge returns a passing adequacy verdict (recorded-from-real GLM, 0.9). The
    invariant: a refusal/empty must STILL terminate as quality_gate_passed=false
    with a non-pass terminal classification, never emitting delegation-completed.
    """

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        contract_path = _bifrost_fixture(_BIFROST_CODE_GENERATION, tmp_path)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _reset_routing_cache()

    @pytest.fixture
    def workflow(self) -> HandlerDelegationWorkflow:
        return HandlerDelegationWorkflow(workflows={})

    @pytest.fixture
    def code_request(self) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Implement an add(a, b) function returning a + b.",
            task_type="code_generation",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )

    async def _run_full_chain(
        self,
        workflow: HandlerDelegationWorkflow,
        request: ModelDelegationRequest,
        llm_content: str,
    ) -> tuple[ModelQualityGateResult, list[BaseModel]]:
        """Drive orchestrator -> routing -> inference -> gate(async judge) -> terminal.

        Every hop is the real handler. httpx is patched at the inference hop to
        inject the candidate body; the gate hop runs the async judge-combine path
        over the recorded GLM replay (concrete model id pinned).
        """
        publisher = _CapturingPublisher()
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent(
            judge=HandlerJudgeAdequacy(inference_bridge=RecordedJudgeReplayAdapter())
        )

        # Hop 1: orchestrator emits routing intent.
        routing_intents = workflow.handle_delegation_request(request)
        assert len(routing_intents) == 1
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        # Hop 2: routing reducer -> ModelRoutingDecision (REAL resolution).
        decision = routing_handler.handle(routing_intents[0])
        publisher.publish(TOPIC_ROUTING_DECISION, decision)

        # Hop 3: orchestrator emits inference intent.
        inference_intents = workflow.handle_routing_decision(decision)
        assert len(inference_intents) == 1
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        # Hop 4: LLM call effect (httpx patched) -> ModelInferenceResponseData.
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response(llm_content)
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])
        publisher.publish(TOPIC_INFERENCE_RESPONSE, response)

        # Hop 5: orchestrator emits quality gate intent.
        gate_intents = workflow.handle_inference_response(response)
        assert len(gate_intents) == 1
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        # Hop 6: quality gate reducer with the OMN-13470 judge combine ACTIVE.
        gate_output = await gate_handler.handle_async(gate_intents[0])
        gate_result = next(
            e for e in gate_output.events if isinstance(e, ModelQualityGateResult)
        )
        # The judge EFFECT must actually have run on the canonical inference path.
        assert any(
            isinstance(e, ModelDelegationJudgeVerdictEvent) for e in gate_output.events
        ), "the OMN-13470 judge verdict event must be emitted (combine active)"
        publisher.publish(TOPIC_QUALITY_GATE_RESULT, gate_result)

        # Hop 7: orchestrator processes the gate result -> terminal events.
        terminal_events = workflow.handle_gate_result(gate_result)

        return gate_result, terminal_events

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "llm_content",
        [_REFUSAL_PHRASE, _REFUSAL_SOFT, _REFUSAL_SHORT, _INADEQUATE_CODE],
    )
    async def test_refusal_or_inadequate_not_completed(
        self,
        workflow: HandlerDelegationWorkflow,
        code_request: ModelDelegationRequest,
        llm_content: str,
    ) -> None:
        gate_result, terminal_events = await self._run_full_chain(
            workflow, code_request, llm_content
        )

        assert gate_result.passed is False, (
            f"code_generation refusal/inadequate {llm_content!r} must fail the gate "
            f"even with the judge combine active; score={gate_result.quality_score}, "
            f"score_source={gate_result.score_source}, reasons={gate_result.failure_reasons}"
        )
        assert gate_result.fail_category != "pass", (
            f"non-pass terminal classification required; got {gate_result.fail_category}"
        )
        # The deterministic floor must block before the judge combine — score_source
        # never reaches "combined" for a refusal/empty.
        assert gate_result.score_source != "combined", (
            "a refusal/empty must hard-block before the judge combine; "
            f"got score_source={gate_result.score_source}"
        )

        topics = _terminal_topics(terminal_events)
        assert TOPIC_ID_DELEGATION_COMPLETED not in topics, (
            f"delegation-completed must NOT be emitted for {llm_content!r}; topics={topics}"
        )
        state = workflow.workflows[code_request.correlation_id].state
        assert state in {EnumDelegationState.FAILED, EnumDelegationState.ROUTED}, (
            f"workflow must be FAILED or ROUTED (escalation) after a refusal/empty; "
            f"got state={state}"
        )


@pytest.mark.unit
class TestResearchRefusalRealDispatchPath:
    """Drive the full orchestrator -> quality gate dispatch path for research.

    research is NOT a judge-combinable class, so the real gate hop is the
    synchronous ``handle`` — the production path for research. A refusal fails the
    no_refusal heuristic and an empty fails response_non_empty; both terminate as
    quality_gate_passed=false with a non-pass classification, never completing.
    """

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> Generator[None, None, None]:
        contract_path = _bifrost_fixture(_BIFROST_RESEARCH, tmp_path)
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _reset_routing_cache()

    @pytest.fixture
    def workflow(self) -> HandlerDelegationWorkflow:
        return HandlerDelegationWorkflow(workflows={})

    @pytest.fixture
    def research_request(self) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Research how the delegation quality gate floors refusals.",
            task_type="research",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )

    def _run_full_chain(
        self,
        workflow: HandlerDelegationWorkflow,
        request: ModelDelegationRequest,
        llm_content: str,
    ) -> tuple[ModelQualityGateResult, list[BaseModel]]:
        publisher = _CapturingPublisher()
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent()

        routing_intents = workflow.handle_delegation_request(request)
        assert len(routing_intents) == 1
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        decision = routing_handler.handle(routing_intents[0])
        publisher.publish(TOPIC_ROUTING_DECISION, decision)

        inference_intents = workflow.handle_routing_decision(decision)
        assert len(inference_intents) == 1
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response(llm_content)
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])
        publisher.publish(TOPIC_INFERENCE_RESPONSE, response)

        gate_intents = workflow.handle_inference_response(response)
        assert len(gate_intents) == 1
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        gate_result = gate_handler.handle(gate_intents[0])
        publisher.publish(TOPIC_QUALITY_GATE_RESULT, gate_result)

        terminal_events = workflow.handle_gate_result(gate_result)
        return gate_result, terminal_events

    @pytest.mark.parametrize(
        "llm_content", [_REFUSAL_PHRASE, _REFUSAL_SOFT, _REFUSAL_SHORT]
    )
    def test_refusal_not_completed(
        self,
        workflow: HandlerDelegationWorkflow,
        research_request: ModelDelegationRequest,
        llm_content: str,
    ) -> None:
        gate_result, terminal_events = self._run_full_chain(
            workflow, research_request, llm_content
        )

        assert gate_result.passed is False, (
            f"research refusal {llm_content!r} must fail the gate; "
            f"score={gate_result.quality_score}, reasons={gate_result.failure_reasons}"
        )
        assert gate_result.fail_category != "pass", (
            f"non-pass terminal classification required; got {gate_result.fail_category}"
        )

        topics = _terminal_topics(terminal_events)
        assert TOPIC_ID_DELEGATION_COMPLETED not in topics, (
            f"delegation-completed must NOT be emitted for {llm_content!r}; topics={topics}"
        )
        state = workflow.workflows[research_request.correlation_id].state
        assert state in {EnumDelegationState.FAILED, EnumDelegationState.ROUTED}, (
            f"workflow must be FAILED or ROUTED (escalation) after a refusal; "
            f"got state={state}"
        )

    def test_inadequate_not_completed(
        self,
        workflow: HandlerDelegationWorkflow,
        research_request: ModelDelegationRequest,
    ) -> None:
        """A thin, unsupported research answer must fail the gate, not complete.

        ``"It works fine."`` carries no sources and no reasoning structure, so it
        fails the research heuristics (cites_sources / methodical_analysis) and
        terminates as quality_gate_passed=false with a non-pass classification.
        """
        gate_result, terminal_events = self._run_full_chain(
            workflow, research_request, _INADEQUATE_RESEARCH
        )

        assert gate_result.passed is False, (
            f"inadequate research answer must fail the gate; "
            f"score={gate_result.quality_score}, reasons={gate_result.failure_reasons}"
        )
        assert gate_result.fail_category != "pass", (
            f"non-pass terminal classification required; got {gate_result.fail_category}"
        )

        topics = _terminal_topics(terminal_events)
        assert TOPIC_ID_DELEGATION_COMPLETED not in topics, (
            f"delegation-completed must NOT be emitted for an inadequate answer; "
            f"topics={topics}"
        )
        state = workflow.workflows[research_request.correlation_id].state
        assert state in {EnumDelegationState.FAILED, EnumDelegationState.ROUTED}
