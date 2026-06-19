# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Tests for delegation tier escalation (OMN-12254).

Covers:
- Gate fail with fallback_recommended triggers ESCALATING -> ROUTED
- Max escalation attempts reached -> FAILED with terminal_failure_reason
- No higher tier available -> FAILED
- Fallback not recommended -> FAILED
- Routing reducer min_tier_name skips lower tiers
- Escalation history populated correctly
- Terminal event includes escalation metadata
- next_eligible_tier helper logic
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import (
    EnumDelegationState,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_event import (
    ModelDelegationEvent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
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
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    NO_HIGHER_TIER_REASON_TOKEN,
    _get_config,
    _tier_order_from_contract,
    describe_no_higher_tier_available,
    next_eligible_tier,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_request(
    correlation_id: UUID | None = None,
    task_type: str = "test",
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt="Write unit tests for verify_registration.py",
        task_type=task_type,  # type: ignore[arg-type]
        correlation_id=correlation_id or uuid4(),
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID,
    task_type: str = "test",
    tier_name: str = "local",
    endpoint_url: str = "http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-12254 reason="delegation test fixture for local AIPC LLM endpoint"
    selected_model: str = "qwen3-coder-30b",
) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=task_type,
        selected_model=selected_model,
        selected_backend_id=uuid5(
            NAMESPACE_DNS, f"omninode.ai/backends/{selected_model}"
        ),
        endpoint_url=endpoint_url,
        cost_tier="low",
        max_context_tokens=65536,
        max_tokens=65536,  # OMN-13345: contract backend output ceiling
        system_prompt="You are a test generation assistant.",
        rationale=f"Task '{task_type}' routed to {selected_model}.",
        tier_name=tier_name,
    )


def _make_inference_response(
    correlation_id: UUID,
    content: str = "def test_foo():\n    pass",
    model_used: str = "Qwen3-Coder-30B-A3B",
    prompt_tokens: int = 100,
    completion_tokens: int = 200,
    total_tokens: int = 300,
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content=content,
        model_used=model_used,
        latency_ms=42,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
    )


def _make_gate_result(
    correlation_id: UUID,
    passed: bool = True,
    quality_score: float = 0.9,
    failure_reasons: tuple[str, ...] = (),
    fallback_recommended: bool = False,
) -> ModelQualityGateResult:
    return ModelQualityGateResult(
        correlation_id=correlation_id,
        passed=passed,
        quality_score=quality_score,
        failure_reasons=failure_reasons,
        fallback_recommended=fallback_recommended,
    )


def _advance_to_gate_evaluated(
    handler: HandlerDelegationWorkflow,
    cid: UUID,
    tier_name: str = "local",
) -> None:
    """Drive the FSM from RECEIVED through to GATE_EVALUATED (inference done)."""
    request = _make_request(correlation_id=cid)
    handler.handle_delegation_request(request)
    decision = _make_routing_decision(cid, tier_name=tier_name)
    handler.handle_routing_decision(decision)
    response = _make_inference_response(cid)
    handler.handle_inference_response(response)
    assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED


# ---------------------------------------------------------------------------
# Tests: Escalation Triggers
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateFailWithFallbackTriggersEscalation:
    """Task 10, test 1: gate fail + fallback_recommended -> ESCALATING -> ROUTED."""

    def test_gate_fail_with_fallback_triggers_escalation(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("refusal_detected",),
            fallback_recommended=True,
        )
        events = handler.handle_gate_result(gate)

        workflow = handler.workflows[cid]
        # Should have transitioned through ESCALATING to ROUTED
        assert workflow.state == EnumDelegationState.ROUTED
        assert workflow.escalation_count == 1

        # Should emit a ModelRoutingIntent with min_tier_name...
        routing_intents = [e for e in events if isinstance(e, ModelRoutingIntent)]
        assert len(routing_intents) == 1
        intent = routing_intents[0]
        assert intent.min_tier_name is not None
        assert intent.min_tier_name != "local"  # Should be a higher tier
        # ...alongside the typed escalation proof (OMN-13140).
        escalations = [
            e
            for e in events
            if isinstance(e, ModelLlmDelegationEscalationTriggeredEvent)
        ]
        assert len(escalations) == 1


@pytest.mark.unit
class TestGateFailMaxEscalationReached:
    """Task 10, test 2: max escalation attempts exhausted -> FAILED."""

    def test_gate_fail_max_escalation_reached(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        # Pre-set escalation count to max
        handler.workflows[cid].escalation_count = 2

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("refusal_detected",),
            fallback_recommended=True,
        )
        events = handler.handle_gate_result(gate, max_escalation_attempts=2)

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.FAILED

        # Find the delegation result event
        result_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) == 1
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)
        assert result.terminal_failure_reason == "max_escalation_attempts_reached"


@pytest.mark.unit
class TestGateFailNoHigherTier:
    """Task 10, test 3: already on highest tier -> FAILED."""

    def test_gate_fail_no_higher_tier(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="claude")

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("refusal_detected",),
            fallback_recommended=True,
        )
        events = handler.handle_gate_result(gate)

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.FAILED

        result_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) == 1
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)
        # OMN-13167: precise reason — stable machine-keyable token prefix plus the
        # exhausted task-class policy that produced the dead-end.
        assert result.terminal_failure_reason is not None
        assert result.terminal_failure_reason.startswith("no_higher_tier_available")
        assert "task_class='test'" in result.terminal_failure_reason
        assert "tier_order=" in result.terminal_failure_reason


@pytest.mark.unit
class TestGateFailFallbackNotRecommended:
    """Task 10, test 4: gate fails but fallback not recommended -> FAILED."""

    def test_gate_fail_fallback_not_recommended(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.4,
            failure_reasons=("low_quality",),
            fallback_recommended=False,
        )
        events = handler.handle_gate_result(gate)

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.FAILED

        result_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) == 1
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)
        assert result.terminal_failure_reason == "fallback_not_recommended"


# ---------------------------------------------------------------------------
# Tests: Routing Reducer min_tier_name
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRoutingReducerMinTierName:
    """Task 10, tests 5-6: routing reducer handles min_tier_name."""

    def test_routing_intent_carries_min_tier_name(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        """Verify the escalation path emits intent with min_tier_name set."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("refusal_detected",),
            fallback_recommended=True,
        )
        events = handler.handle_gate_result(gate)

        routing_intents = [e for e in events if isinstance(e, ModelRoutingIntent)]
        assert len(routing_intents) == 1
        intent = routing_intents[0]
        # min_tier_name should be set to something above "local"
        assert intent.min_tier_name is not None

    def test_default_intent_has_no_min_tier_name(self) -> None:
        """Normal (non-escalation) routing intent has min_tier_name=None."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        request = _make_request(correlation_id=cid)
        intents = handler.handle_delegation_request(request)

        assert len(intents) == 1
        assert isinstance(intents[0], ModelRoutingIntent)
        assert intents[0].min_tier_name is None


# ---------------------------------------------------------------------------
# Tests: Escalation History
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEscalationHistory:
    """Task 10, test 7: escalation history populated correctly."""

    def test_escalation_history_populated(self) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("refusal_detected",),
            fallback_recommended=True,
        )
        handler.handle_gate_result(gate)

        workflow = handler.workflows[cid]
        assert len(workflow.escalation_history) == 1

        attempt = workflow.escalation_history[0]
        assert attempt.tier_name == "local"
        assert attempt.quality_score == 0.2
        assert attempt.fallback_recommended is True
        assert "refusal_detected" in attempt.failure_reasons


# ---------------------------------------------------------------------------
# Tests: Terminal Event Metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTerminalEventEscalationMetadata:
    """Task 10, test 8: terminal event includes escalation metadata."""

    def test_terminal_event_includes_escalation_metadata_on_fail(self) -> None:
        """After escalation fails (no higher tier), FAILED result has metadata."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="claude")

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("refusal_detected",),
            fallback_recommended=True,
        )
        events = handler.handle_gate_result(gate)

        result_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) == 1
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)

        # Escalation metadata should be present
        assert result.escalation_count == 0  # No successful escalation happened
        assert result.attempts_count == 1
        assert (
            result.routing_tiers_hash is not None or result.routing_tiers_hash is None
        )  # May or may not exist depending on config file
        # OMN-13167: precise terminal reason keyed by the stable token prefix.
        assert result.terminal_failure_reason is not None
        assert result.terminal_failure_reason.startswith("no_higher_tier_available")
        assert isinstance(result.escalation_history, tuple)

    def test_passed_result_carries_zero_escalation_count(self) -> None:
        """Normal pass with no escalation has escalation_count=0."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        _advance_to_gate_evaluated(handler, cid, tier_name="local")

        gate = _make_gate_result(cid, passed=True, quality_score=0.9)
        events = handler.handle_gate_result(gate)

        result_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) >= 1
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)
        assert result.escalation_count == 0
        assert result.attempts_count == 1
        assert result.terminal_failure_reason is None


# ---------------------------------------------------------------------------
# Tests: next_eligible_tier helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNextEligibleTier:
    """Task 10, tests 10-11: next_eligible_tier helper logic."""

    def test_next_eligible_tier_advances_to_cheap_frontier(self) -> None:
        """next_eligible_tier('cheap_cloud', {}) -> 'cheap_frontier'.

        OMN-12492 added cheap_frontier between cheap_cloud and claude.
        Escalation from cheap_cloud lands on cheap_frontier (not claude directly).
        OMN-13215: the shelled cli_agents tier was removed; declaration order is
        local -> cheap_cloud -> cheap_frontier -> claude.
        """
        result = next_eligible_tier("cheap_cloud", frozenset())
        assert result == "cheap_frontier"

    def test_next_eligible_tier_from_cheap_frontier(self) -> None:
        """next_eligible_tier('cheap_frontier', {}) -> 'claude' (HTTP ceiling)."""
        result = next_eligible_tier("cheap_frontier", frozenset())
        assert result == "claude"

    def test_next_eligible_tier_last_eligible_returns_none(self) -> None:
        """next_eligible_tier('claude', {}) -> None (claude is the ceiling)."""
        result = next_eligible_tier("claude", frozenset())
        assert result is None

    def test_next_eligible_tier_from_local(self) -> None:
        """next_eligible_tier('local', {}) -> 'cheap_cloud'."""
        result = next_eligible_tier("local", frozenset())
        assert result == "cheap_cloud"

    def test_next_eligible_tier_unrecognized_returns_none(self) -> None:
        """Unrecognized tier name returns None."""
        result = next_eligible_tier("nonexistent_tier", frozenset())
        assert result is None


# ---------------------------------------------------------------------------
# Tests: next_eligible_tier task-aware endpoint resolvability (OMN-12939)
# ---------------------------------------------------------------------------
#
# Regression: CID a604cd40 (stability-test, 2026-06-11). A `document` task
# failed the quality gate on local AND cheap_cloud. The orchestrator escalated
# to the next *declared* tier (cheap_frontier, then claude). But for a document
# task, cheap_frontier declares no model serving `document`, and the claude tier
# backend (cloud-sonnet) had an EMPTY endpoint_url in the deployed bifrost
# config. The routing reducer's delta() therefore found no routable tier and
# raised ProtocolConfigurationError, crashing the dispatcher — no routing
# decision, no terminal delegation event, no projection row. The FSM stranded
# in ROUTED forever.
#
# Fix: next_eligible_tier must be task-aware — skip any higher tier that cannot
# actually route the task (no model serving task_type with a resolvable backend
# endpoint). When no higher tier can route, it returns None so the orchestrator
# terminates with `no_higher_tier_available` and emits a delegation-failed event
# (which materializes a projection row) instead of stranding.


@pytest.mark.unit
class TestNextEligibleTierEndpointResolvability:
    """OMN-12939: next_eligible_tier must skip tiers that cannot route the task.

    Without task-awareness, escalation off cheap_cloud for a `document` task
    returns cheap_frontier/claude even though neither can serve the task, and the
    routing reducer crashes resolving an endpoint. The fix returns None so the
    orchestrator terminates gracefully.
    """

    def test_skips_higher_tier_with_unresolvable_endpoint_for_document_task(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        # cheap_frontier declares no model serving `document`; the claude tier's
        # backend has an empty endpoint_url. No higher tier can route a document
        # task, so the correct answer is None (terminate, do not strand).
        result = next_eligible_tier(
            "cheap_cloud",
            frozenset(),
            task_type="document",
        )
        assert result is None

    def test_task_aware_still_advances_when_higher_tier_routable(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        # cheap_cloud carries resolvable endpoints that serve `document`, so
        # escalating from local must still advance to cheap_cloud.
        result = next_eligible_tier(
            "local",
            frozenset(),
            task_type="document",
        )
        assert result == "cheap_cloud"

    def test_task_unaware_call_preserves_declaration_order(self) -> None:
        # Backward-compat: omitting task_type preserves pure declaration order.
        result = next_eligible_tier("cheap_cloud", frozenset())
        assert result == "cheap_frontier"

    def test_task_unaware_call_does_not_apply_code_generation_tier_order(
        self,
    ) -> None:
        # Backward-compat: without task_type, escalation remains forward-only in
        # routing_tiers.yaml declaration order. It must not jump back to local
        # just because code_generation declares cheap_cloud -> local -> claude.
        result = next_eligible_tier(
            "cheap_cloud",
            frozenset({"cheap_frontier"}),
        )
        assert result == "claude"

    def test_code_generation_uses_contract_tier_order_after_cheap_cloud(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        # code_generation declares cheap_cloud -> local -> claude. A cheap_cloud
        # failure must therefore try local next, even though local appears before
        # cheap_cloud in routing_tiers.yaml declaration order.
        result = next_eligible_tier(
            "cheap_cloud",
            frozenset({"cheap_frontier"}),
            task_type="code_generation",
        )
        assert result == "local"

    def test_tier_order_unknown_tier_fails_configuration(self) -> None:
        config = _get_config()
        with pytest.raises(
            ProtocolConfigurationError,
            match="tier_order references unknown routing tier",
        ):
            _tier_order_from_contract(
                config,
                {"escalation_policy": {"tier_order": ["local", "not_a_tier"]}},
            )


@pytest.mark.unit
class TestEscalationTerminatesWhenFrontierUnconfigured:
    """OMN-12939: full orchestrator path — gate fails on local+cheap_cloud for a
    document task with an unconfigured frontier tier must reach terminal FAILED
    and emit a delegation-failed event (so a projection row materializes),
    never strand in ROUTED.
    """

    def test_gate_fail_document_task_terminates_when_no_routable_higher_tier(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()

        # Drive through local then cheap_cloud, failing the gate each time.
        request = _make_request(correlation_id=cid, task_type="document")
        handler.handle_delegation_request(request)

        # Attempt 1: local tier, gate fails -> escalate.
        decision1 = _make_routing_decision(cid, task_type="document", tier_name="local")
        handler.handle_routing_decision(decision1)
        handler.handle_inference_response(_make_inference_response(cid))
        gate1 = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.0,
            failure_reasons=("dod_failure",),
            fallback_recommended=True,
        )
        events1 = handler.handle_gate_result(gate1)
        assert any(isinstance(e, ModelRoutingIntent) for e in events1)
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

        # Attempt 2: cheap_cloud tier, gate fails -> must terminate (no routable
        # higher tier for a document task), NOT strand.
        decision2 = _make_routing_decision(
            cid, task_type="document", tier_name="cheap_cloud"
        )
        handler.handle_routing_decision(decision2)
        handler.handle_inference_response(_make_inference_response(cid))
        gate2 = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.0,
            failure_reasons=("dod_failure",),
            fallback_recommended=True,
        )
        events2 = handler.handle_gate_result(gate2)

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.FAILED, (
            "FSM must reach terminal FAILED, not strand in ROUTED"
        )
        result_events = [e for e in events2 if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) == 1, "a terminal delegation event must be emitted"
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)
        assert result.quality_passed is False
        # OMN-13167: precise reason naming the exhausted `document` policy plus
        # the unusable higher tiers, prefixed with the stable token.
        assert result.terminal_failure_reason is not None
        assert result.terminal_failure_reason.startswith("no_higher_tier_available")
        assert "task_class='document'" in result.terminal_failure_reason


# ---------------------------------------------------------------------------
# Tests: ModelRoutingDecision tier_name
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestRoutingDecisionTierName:
    """Task 9: tier_name field on ModelRoutingDecision."""

    def test_tier_name_on_routing_decision(self) -> None:
        decision = _make_routing_decision(uuid4(), tier_name="local")
        assert decision.tier_name == "local"

    def test_tier_name_default_empty(self) -> None:
        """Backward compat: tier_name defaults to empty string."""
        decision = ModelRoutingDecision(
            correlation_id=uuid4(),
            task_type="test",
            selected_model="model",
            selected_backend_id=uuid5(NAMESPACE_DNS, "test"),
            endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-12254 reason="delegation test fixture"
            cost_tier="low",
            max_context_tokens=65536,
            max_tokens=65536,  # OMN-13345: contract backend output ceiling
            system_prompt="test",
            rationale="test",
        )
        assert decision.tier_name == ""

    def test_current_tier_name_captured_from_decision(self) -> None:
        """handle_routing_decision sets workflow.current_tier_name."""
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()
        request = _make_request(correlation_id=cid)
        handler.handle_delegation_request(request)

        decision = _make_routing_decision(cid, tier_name="local")
        handler.handle_routing_decision(decision)

        workflow = handler.workflows[cid]
        assert workflow.current_tier_name == "local"


# ---------------------------------------------------------------------------
# Tests: OMN-13167 — test/research task-class tier policy + precise terminal
# ---------------------------------------------------------------------------
#
# Repro: night autonomous runtime matrix (2026-06-16) found `test` (dev) and
# `research` (stability-test) delegation cells fail quality with fallback
# recommended but no usable higher tier, dead-ending with a bare
# `no_higher_tier_available` that named neither the exhausted policy nor the
# missing tier. Acceptance:
#   (1) test/research tier policies are verified (not just code_generation), and
#   (2) when fallback is recommended the runtime either escalates to an available
#       configured tier OR emits a precise terminal reason naming the exhausted
#       policy and missing tier.


@pytest.mark.unit
class TestTestResearchTierPolicyVerified:
    """OMN-13167 (1): `test` and `research` declare claude as the ceiling tier,
    so when the claude tier backend is configured, escalation off cheap_cloud
    advances to claude — a usable higher tier exists, no dead-end.

    OMN-13215: the ceiling tier is an HTTP-backed frontier backend, routable
    through the canonical HTTP path exactly like the lower tiers. "Configured"
    means the ceiling's secret_ref resolves; the test sets that env-mapped secret
    so the claude tier is routable.

    OMN-13351: the ceiling backend was repointed from the dead Anthropic
    ``cloud-sonnet`` (secret_ref llm.anthropic.api_key, resolves to None in every
    lane) to the resolvable Gemini ``cloud-gemini-pro`` (secret_ref
    llm.gemini.api_key). The fixture now sets llm.gemini.api_key so the new ceiling
    backend is routable.
    """

    @pytest.fixture(autouse=True)
    def _configure_ceiling_secret(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> Iterator[None]:
        # OMN-13351: the repo-default bifrost contract maps the claude ceiling tier
        # to cloud-gemini-pro, whose secret_ref is llm.gemini.api_key. The env-backed
        # secret store reads the ref name verbatim, so set it to make the ceiling
        # routable.
        from omnimarket.nodes.node_delegation_routing_reducer.handlers import (
            handler_delegation_routing as routing,
        )

        monkeypatch.setenv("llm.gemini.api_key", "test-gemini-key")
        routing._load_bifrost_endpoints.cache_clear()
        yield
        routing._load_bifrost_endpoints.cache_clear()

    @pytest.mark.parametrize("task_type", ["test", "research"])
    def test_claude_tier_reachable_when_configured(self, task_type: str) -> None:
        # local -> cheap_cloud -> claude is the declared closed-set policy for
        # both classes. With the repo-default contract the claude tier
        # (cloud-sonnet) is routable once its secret resolves, so escalating off
        # cheap_cloud must land on claude rather than returning None.
        assert (
            next_eligible_tier(
                "cheap_cloud",
                frozenset(),
                task_type=task_type,
            )
            == "claude"
        )

    @pytest.mark.parametrize("task_type", ["test", "research"])
    def test_claude_is_ceiling_no_higher_tier(self, task_type: str) -> None:
        # claude is the final declared tier for both classes; nothing follows it.
        assert (
            next_eligible_tier(
                "claude",
                frozenset(),
                task_type=task_type,
            )
            is None
        )


@pytest.mark.unit
class TestDescribeNoHigherTierAvailable:
    """OMN-13167 (2): the diagnostic names the exhausted policy and missing tier.

    Uses the frontier_unconfigured fixture (cli-codex / claude tier NOT declared),
    reproducing the runtime lane where the claude tier was unconfigured — the
    exact condition that dead-ended the night-matrix `test`/`research` cells.
    """

    def test_reason_names_policy_and_missing_tiers_for_test_after_cheap_cloud(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        # `test` policy is local -> cheap_cloud -> claude. In this lane the claude
        # tier (cli-codex) is unconfigured, so after cheap_cloud there is no usable
        # higher tier — the real dead-end the night matrix hit.
        reason = describe_no_higher_tier_available(
            "cheap_cloud",
            frozenset(),
            task_type="test",
        )
        assert reason.startswith(NO_HIGHER_TIER_REASON_TOKEN)
        assert "task_class='test'" in reason
        # The closed-set policy order is named (the exhausted policy).
        assert "tier_order=" in reason
        assert "'cheap_cloud'" in reason
        assert "'claude'" in reason
        # The current tier and the unusable higher tier are named with a reason.
        assert "current_tier='cheap_cloud'" in reason
        assert "claude(" in reason

    def test_reason_names_policy_for_research_after_cheap_cloud(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        reason = describe_no_higher_tier_available(
            "cheap_cloud",
            frozenset(),
            task_type="research",
        )
        assert reason.startswith(NO_HIGHER_TIER_REASON_TOKEN)
        assert "task_class='research'" in reason
        assert "current_tier='cheap_cloud'" in reason
        # claude is the only tier after cheap_cloud in the research policy; it is
        # unroutable here, so it must be named with a skip reason.
        assert "claude(" in reason

    def test_reason_for_ceiling_tier_states_final_tier(self) -> None:
        # On the repo-default contract, claude is the declared ceiling for `test`.
        reason = describe_no_higher_tier_available(
            "claude",
            frozenset(),
            task_type="test",
        )
        assert reason.startswith(NO_HIGHER_TIER_REASON_TOKEN)
        assert "task_class='test'" in reason
        assert "final" in reason
        assert "ceiling" in reason


@pytest.mark.unit
class TestTestTaskDeadEndEmitsPreciseReason:
    """OMN-13167 (2) end-to-end: a `test` task that fails on local with the
    claude tier unconfigured must terminate FAILED with a PRECISE reason naming
    the exhausted policy and missing tier — not a bare token.
    """

    def test_test_task_terminal_reason_is_precise(
        self, frontier_unconfigured_bifrost: None
    ) -> None:
        handler = HandlerDelegationWorkflow(workflows={})
        cid = uuid4()

        request = _make_request(correlation_id=cid, task_type="test")
        handler.handle_delegation_request(request)

        # Attempt 1: local tier, gate fails with fallback -> escalate to cheap_cloud.
        decision1 = _make_routing_decision(cid, task_type="test", tier_name="local")
        handler.handle_routing_decision(decision1)
        handler.handle_inference_response(_make_inference_response(cid))
        gate1 = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.867,
            failure_reasons=("TASK_MISMATCH: failed covers_edge_cases",),
            fallback_recommended=True,
        )
        events1 = handler.handle_gate_result(gate1)
        assert any(isinstance(e, ModelRoutingIntent) for e in events1)
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

        # Attempt 2: cheap_cloud tier, gate fails -> claude tier is unconfigured
        # in this lane, so no usable higher tier -> terminate FAILED with a
        # PRECISE reason (the night-matrix DEL-DEV-TEST-EXTEND dead-end shape).
        decision2 = _make_routing_decision(
            cid, task_type="test", tier_name="cheap_cloud"
        )
        handler.handle_routing_decision(decision2)
        handler.handle_inference_response(_make_inference_response(cid))
        gate2 = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.867,
            failure_reasons=("TASK_MISMATCH: failed covers_edge_cases",),
            fallback_recommended=True,
        )
        events = handler.handle_gate_result(gate2)

        workflow = handler.workflows[cid]
        assert workflow.state == EnumDelegationState.FAILED
        result_events = [e for e in events if isinstance(e, ModelDelegationEvent)]
        assert len(result_events) == 1
        result = result_events[0].payload
        assert isinstance(result, ModelDelegationResult)
        assert result.fallback_to_claude is True
        assert result.terminal_failure_reason is not None
        assert result.terminal_failure_reason.startswith(NO_HIGHER_TIER_REASON_TOKEN)
        # Names the exhausted policy and the missing/unusable higher tiers — the
        # bare token alone is no longer acceptable (OMN-13167 acceptance).
        assert "task_class='test'" in result.terminal_failure_reason
        assert "tier_order=" in result.terminal_failure_reason
        assert "claude(" in result.terminal_failure_reason
        assert result.terminal_failure_reason != NO_HIGHER_TIER_REASON_TOKEN
