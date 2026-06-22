# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# onex-allow-file OMN-12294 reason="delegation e2e test must reference exact local HF model IDs to verify routing decisions"
"""OMN-13409: summarization refusal must NOT pass the delegation quality gate.

Dogfood repro (cid 758871f8): a summarization delegation whose LLM response is a
literal refusal ('NO', 2 tokens) was recorded as delegation-completed with
quality_passed=True and quality_score~=0.9.

Root cause: handler_delegation_workflow.handle_gate_result accepted heuristic-only
failures as passing when the quality score was at or above the required bar (0.8
for summarization). A minimal refusal like 'No.' passes all declared heuristics
(no_refusal only detects specific phrases; semantic_adequacy passes a terminated
single word; concise/accurate have nothing to object to) so the gate produced
passed=True, quality_score=1.0, and the orchestrator emitted delegation-completed.

Fix: extend _check_no_refusal to detect token-count-bounded negation-only /
content-free ultra-short responses (<=5 words, zero task content) as a REFUSAL
pre-filter on the heuristic path. This fires before LLM-based scoring and tags
the result as passed=False, fail_category=fail_heuristic, fallback_recommended=True.
"""

from __future__ import annotations

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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_routing_intent import (
    TOPIC_ROUTING_DECISION,
    HandlerRoutingIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    TOPIC_INFERENCE_RESPONSE,
    HandlerInferenceIntent,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "configs"
    / "task_class_contracts.v1.yaml"
)

# A bifrost contract that maps the cloud-gemini-flash backend_id (which
# routing_tiers.yaml assigns to summarization in the cheap_cloud tier) to a
# test endpoint URL so the routing reducer can resolve a decision without
# requiring live credentials.
_BIFROST_SUMMARIZATION = (
    "config_version: '2.0.0'\n"
    "schema_version: bifrost_delegation.v1\n"
    "backends:\n"
    "  - backend_id: cloud-gemini-flash\n"
    '    endpoint_url: "http://test-summarizer:8000/v1/chat/completions"\n'
    '    model_name: "gemini-2.5-flash-lite"\n'
    "    tier: cheap_cloud\n"
    "    timeout_ms: 30000\n"
    "    capabilities: [summarization, simple_tasks, document, code_generation]\n"
    "routing_rules:\n"
    '  - rule_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"\n'
    "    priority: 10\n"
    "    task_class: summarization\n"
    '    task_class_contract_version: "1.0.0"\n'
    '    backend_policy_version: "2.0.0"\n'
    "    match_operation_types: [chat_completion]\n"
    "    match_capabilities: [summarization]\n"
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

    def last(self, topic: str) -> object:
        for t, payload in reversed(self.published):
            if t == topic:
                return payload
        raise AssertionError(f"no payload published to {topic}: {self.published}")

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


# ---------------------------------------------------------------------------
# Unit-level quality-gate tests (no bus/orchestrator)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefusalDetectionUnit:
    """Unit tests for refusal pre-pass in the quality gate delta function.

    These tests drive handler_quality_gate.delta directly with the canonical
    summarization DoD, reproducing the defect that allowed soft refusals to
    produce passed=True.
    """

    def _dod_inputs(self, content: str) -> ModelQualityGateInput:
        det, heur = _task_class_dod("summarization")
        return ModelQualityGateInput(
            correlation_id=uuid4(),
            task_type="summarization",
            llm_response_content=content,
            dod_deterministic=det,
            dod_heuristic=heur,
        )

    def test_dogfood_repro_no_single_word_fails(self) -> None:
        """Dogfood repro (cid 758871f8): 'NO' (2 tokens) must not pass."""
        result = quality_gate_delta(self._dod_inputs("NO"))

        assert result.passed is False, (
            f"'NO' refusal must not pass the gate; got passed={result.passed}, "
            f"score={result.quality_score}, reasons={result.failure_reasons}"
        )

    def test_no_period_soft_refusal_fails(self) -> None:
        """'No.' is a soft refusal — terminated single word with no task content."""
        result = quality_gate_delta(self._dod_inputs("No."))

        assert result.passed is False, (
            f"'No.' soft refusal must not pass the gate; got passed={result.passed}, "
            f"score={result.quality_score}, reasons={result.failure_reasons}"
        )
        assert any(
            "REFUSAL" in r or "WEAK_OUTPUT" in r for r in result.failure_reasons
        ), (
            f"'No.' must fail with REFUSAL or WEAK_OUTPUT reason; "
            f"got {result.failure_reasons}"
        )

    def test_cannot_fulfill_refusal_fails(self) -> None:
        """'This request cannot be fulfilled.' must not pass as a summarization."""
        result = quality_gate_delta(
            self._dod_inputs("This request cannot be fulfilled.")
        )

        assert result.passed is False, (
            f"refusal 'This request cannot be fulfilled.' must not pass; "
            f"got passed={result.passed}, score={result.quality_score}"
        )

    def test_unable_to_complete_refusal_fails(self) -> None:
        """'Unable to complete this task.' must not pass as a summarization."""
        result = quality_gate_delta(self._dod_inputs("Unable to complete this task."))

        assert result.passed is False, (
            f"refusal 'Unable to complete this task.' must not pass; "
            f"got passed={result.passed}, score={result.quality_score}"
        )

    def test_good_short_summary_still_passes(self) -> None:
        """A correct short summary must still pass (OMN-13218 non-regression)."""
        good = (
            "The change adds a graded quality score so the gate discriminates output."
        )
        result = quality_gate_delta(self._dod_inputs(good))

        assert result.passed is True, (
            f"good short summary must pass; got passed={result.passed}, "
            f"reasons={result.failure_reasons}"
        )

    def test_refusal_fallback_recommended(self) -> None:
        """A detected refusal must set fallback_recommended=True for escalation."""
        result = quality_gate_delta(self._dod_inputs("No."))

        assert result.fallback_recommended is True, (
            f"refusal must recommend fallback; got fallback_recommended="
            f"{result.fallback_recommended}"
        )


# ---------------------------------------------------------------------------
# Real-dispatch-path end-to-end tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRefusalRealDispatchPath:
    """Tests that drive the full orchestrator → quality gate dispatch path.

    Uses the same real-dispatch structure as test_delegation_chain_e2e.py:
    each handler is the REAL handler (not a mock); only httpx is patched to
    return a deterministic LLM response body.
    """

    @pytest.fixture(autouse=True)
    def _bifrost_contract(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing as _h

        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()
        contract_path = tmp_path / "bifrost_delegation.yaml"
        contract_path.write_text(_BIFROST_SUMMARIZATION, encoding="utf-8")
        monkeypatch.setenv("BIFROST_CONTRACT_PATH", str(contract_path))
        yield
        _h._config = None
        _h._load_bifrost_endpoints.cache_clear()

    @pytest.fixture
    def workflow(self) -> HandlerDelegationWorkflow:
        return HandlerDelegationWorkflow(workflows={})

    @pytest.fixture
    def summarization_request(self) -> ModelDelegationRequest:
        return ModelDelegationRequest(
            prompt="Summarize the changes introduced by the quality gate fix.",
            task_type="summarization",
            correlation_id=uuid4(),
            max_tokens=512,
            emitted_at=datetime.now(UTC),
        )

    def _run_full_chain(
        self,
        workflow: HandlerDelegationWorkflow,
        request: ModelDelegationRequest,
        llm_content: str,
    ) -> tuple[object, list[object]]:
        """Drive the real dispatch chain through all 7 hops.

        Returns (gate_result, terminal_events) where terminal_events is the list
        returned by handle_gate_result. Each handler is REAL — only httpx is
        patched to inject the LLM response body.
        """
        publisher = _CapturingPublisher()
        routing_handler = HandlerRoutingIntent()
        inference_handler = HandlerInferenceIntent()
        gate_handler = HandlerQualityGateIntent()

        # Hop 1: orchestrator emits routing intent
        routing_intents = workflow.handle_delegation_request(request)
        assert len(routing_intents) == 1
        assert isinstance(routing_intents[0], ModelRoutingIntent)

        # Hop 2: routing reducer → ModelRoutingDecision
        decision = routing_handler.handle(routing_intents[0])
        publisher.publish(TOPIC_ROUTING_DECISION, decision)

        # Hop 3: orchestrator emits inference intent
        inference_intents = workflow.handle_routing_decision(decision)
        assert len(inference_intents) == 1
        assert isinstance(inference_intents[0], ModelInferenceIntent)

        # Hop 4: LLM call effect (httpx patched) → ModelInferenceResponseData
        with patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = _httpx_response(llm_content)
            mock_client_cls.return_value = mock_client
            response = inference_handler.handle(inference_intents[0])
        publisher.publish(TOPIC_INFERENCE_RESPONSE, response)

        # Hop 5: orchestrator emits quality gate intent
        gate_intents = workflow.handle_inference_response(response)
        assert len(gate_intents) == 1
        assert isinstance(gate_intents[0], ModelQualityGateIntent)

        # Hop 6: quality gate reducer → ModelQualityGateResult
        gate_result = gate_handler.handle(gate_intents[0])
        publisher.publish(TOPIC_QUALITY_GATE_RESULT, gate_result)

        # Hop 7: orchestrator processes gate result → terminal events
        terminal_events = workflow.handle_gate_result(gate_result)

        return gate_result, terminal_events

    def test_dogfood_repro_no_refusal_is_not_completed(
        self,
        workflow: HandlerDelegationWorkflow,
        summarization_request: ModelDelegationRequest,
    ) -> None:
        """OMN-13409 dogfood repro: 'NO' must yield delegation-failed, not -completed.

        Before the fix, 'NO' produced quality_score=0.9 (all heuristics pass
        except semantic_adequacy bare-fragment), but the orchestrator treats
        heuristic-only failures with score>=required_bar as accepted, emitting
        delegation-completed with quality_passed=True. This test pins the
        corrected behavior: a refusal must NEVER emit delegation-completed.
        """
        gate_result, terminal_events = self._run_full_chain(
            workflow, summarization_request, "NO"
        )

        assert gate_result.passed is False, (  # type: ignore[attr-defined]
            f"quality gate must reject 'NO' refusal; got passed={gate_result.passed}, "  # type: ignore[attr-defined]
            f"score={gate_result.quality_score}"  # type: ignore[attr-defined]
        )

        # The terminal events must include delegation-failed, not delegation-completed.
        published_topics = [
            getattr(e, "topic", None)
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
        ]
        assert TOPIC_ID_DELEGATION_COMPLETED not in published_topics, (
            f"delegation-completed must NOT be emitted for a 'NO' refusal; "
            f"topics={published_topics}"
        )
        # The workflow must be FAILED or ROUTED (escalation), never COMPLETED.
        state = workflow.workflows[summarization_request.correlation_id].state
        assert state in {EnumDelegationState.FAILED, EnumDelegationState.ROUTED}, (
            f"workflow must be FAILED or ROUTED after refusal; got state={state}"
        )

    def test_no_period_soft_refusal_is_not_completed(
        self,
        workflow: HandlerDelegationWorkflow,
        summarization_request: ModelDelegationRequest,
    ) -> None:
        """'No.' (a terminated single word) must NOT emit delegation-completed."""
        gate_result, terminal_events = self._run_full_chain(
            workflow, summarization_request, "No."
        )

        assert gate_result.passed is False, (  # type: ignore[attr-defined]
            f"'No.' must not pass; score={gate_result.quality_score}, "  # type: ignore[attr-defined]
            f"reasons={gate_result.failure_reasons}"  # type: ignore[attr-defined]
        )

        published_topics = [
            getattr(e, "topic", None)
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
        ]
        assert TOPIC_ID_DELEGATION_COMPLETED not in published_topics, (
            f"delegation-completed must NOT be emitted for 'No.' refusal; "
            f"topics={published_topics}"
        )
        state = workflow.workflows[summarization_request.correlation_id].state
        assert state in {EnumDelegationState.FAILED, EnumDelegationState.ROUTED}, (
            f"workflow must be FAILED or ROUTED after 'No.' refusal; got state={state}"
        )

    def test_cannot_fulfill_refusal_is_not_completed(
        self,
        workflow: HandlerDelegationWorkflow,
        summarization_request: ModelDelegationRequest,
    ) -> None:
        """'This request cannot be fulfilled.' must NOT emit delegation-completed."""
        gate_result, terminal_events = self._run_full_chain(
            workflow, summarization_request, "This request cannot be fulfilled."
        )

        assert gate_result.passed is False, (  # type: ignore[attr-defined]
            f"must not pass; score={gate_result.quality_score}, "  # type: ignore[attr-defined]
            f"reasons={gate_result.failure_reasons}"  # type: ignore[attr-defined]
        )

        published_topics = [
            getattr(e, "topic", None)
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
        ]
        assert TOPIC_ID_DELEGATION_COMPLETED not in published_topics, (
            f"delegation-completed must NOT be emitted for refusal; "
            f"topics={published_topics}"
        )

    def test_good_summarization_still_completes(
        self,
        workflow: HandlerDelegationWorkflow,
        summarization_request: ModelDelegationRequest,
    ) -> None:
        """A good summarization response must still emit delegation-completed (non-regression)."""
        good_content = (
            "The quality gate fix adds a refusal pre-pass that rejects "
            "content-free ultra-short responses before heuristic scoring."
        )
        gate_result, terminal_events = self._run_full_chain(
            workflow, summarization_request, good_content
        )

        assert gate_result.passed is True, (  # type: ignore[attr-defined]
            f"good summary must pass; got passed={gate_result.passed}, "  # type: ignore[attr-defined]
            f"reasons={gate_result.failure_reasons}"  # type: ignore[attr-defined]
        )

        published_topics = [
            getattr(e, "topic", None)
            for e in terminal_events
            if isinstance(e, ModelDelegationEvent)
        ]
        assert TOPIC_ID_DELEGATION_COMPLETED in published_topics, (
            f"good summary must produce delegation-completed; topics={published_topics}"
        )
        state = workflow.workflows[summarization_request.correlation_id].state
        assert state == EnumDelegationState.COMPLETED
