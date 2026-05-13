# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Tests for the delegation orchestrator FSM handler.

Covers:
- Happy path: request -> route -> infer -> gate pass -> completed
- Gate fail: request -> route -> infer -> gate fail -> failed event
- Duplicate event: same correlation_id twice -> idempotent
- Out-of-order: gate result before inference -> held (no processing)
- Invalid state transitions

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from omnimarket.nodes.node_delegation_orchestrator.enums import (
    EnumDelegationState,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
    InvalidStateTransitionError,
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
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
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
    prompt: str = "Write unit tests for verify_registration.py",
) -> ModelDelegationRequest:
    return ModelDelegationRequest(
        prompt=prompt,
        task_type=task_type,  # type: ignore[arg-type]
        correlation_id=correlation_id or uuid4(),
        emitted_at=datetime.now(UTC),
    )


def _make_routing_decision(
    correlation_id: UUID,
    task_type: str = "test",
) -> ModelRoutingDecision:
    from uuid import NAMESPACE_DNS, uuid5

    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=task_type,
        selected_model="qwen3-coder-30b",
        selected_backend_id=uuid5(
            NAMESPACE_DNS, "omninode.ai/backends/qwen3-coder-30b"
        ),
        endpoint_url="http://192.168.86.201:8000",  # onex-allow-internal-ip OMN-10865 reason="delegation test fixture for local AIPC LLM endpoint"
        cost_tier="low",
        max_context_tokens=65536,
        system_prompt="You are a test generation assistant.",
        rationale="Task 'test' routed to qwen3-coder-30b.",
    )


def _make_inference_response(
    correlation_id: UUID,
    content: str = "def test_foo():\n    pass",
    model_used: str = "Qwen3-Coder-30B-A3B",
    latency_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    llm_call_id: str = "",
) -> ModelInferenceResponseData:
    return ModelInferenceResponseData(
        correlation_id=correlation_id,
        content=content,
        model_used=model_used,
        llm_call_id=llm_call_id,
        latency_ms=latency_ms,
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


# ---------------------------------------------------------------------------
# Tests: Happy Path
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestHappyPath:
    """Test the full happy path: request -> route -> infer -> gate pass -> completed."""

    def test_full_delegation_flow_produces_completed_event(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()
        request = _make_request(correlation_id=cid)

        # Step 1: Handle request -> emits routing intent
        intents = handler.handle_delegation_request(request)
        assert len(intents) == 1
        assert isinstance(intents[0], ModelRoutingIntent)
        assert intents[0].intent == "routing_reducer"
        assert handler.workflows[cid].state == EnumDelegationState.RECEIVED

        # Step 2: Handle routing decision -> emits inference intent
        decision = _make_routing_decision(cid)
        intents = handler.handle_routing_decision(decision)
        assert len(intents) == 1
        assert isinstance(intents[0], ModelInferenceIntent)
        assert intents[0].intent == "llm_inference"
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

        # Step 3: Handle inference response -> emits quality gate intent
        response = _make_inference_response(
            correlation_id=cid,
            content="def test_verify_registration():\n    assert True",
            model_used="Qwen3-Coder-30B-A3B",
            latency_ms=1200,
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            llm_call_id="chatcmpl-abc123",
        )
        intents = handler.handle_inference_response(response)
        assert len(intents) == 1
        assert isinstance(intents[0], ModelQualityGateIntent)
        assert intents[0].intent == "quality_gate"
        assert handler.workflows[cid].state == EnumDelegationState.INFERENCE_COMPLETED

        # Step 4: Handle gate result (pass) -> emits completed + baseline + compat
        gate = _make_gate_result(cid, passed=True, quality_score=0.9)
        intents = handler.handle_gate_result(gate)
        # 3 events: delegation-completed, baseline intent, backward-compat task-delegated
        assert len(intents) == 3
        assert isinstance(intents[0], ModelDelegationEvent)
        assert intents[0].topic == "onex.evt.omnibase-infra.delegation-completed.v1"
        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED

        result: ModelDelegationResult = intents[0].payload
        assert result.correlation_id == cid
        assert result.quality_passed is True
        assert result.quality_score == 0.9
        assert result.model_used == "Qwen3-Coder-30B-A3B"
        assert result.task_type == "test"
        assert result.fallback_to_claude is False
        assert result.failure_reason == ""
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.total_tokens == 150

        # Baseline intent for savings computation (Task 11)
        from omnimarket.nodes.node_delegation_orchestrator.models.model_baseline_intent import (
            ModelBaselineIntent,
        )

        assert isinstance(intents[1], ModelBaselineIntent)
        assert intents[1].correlation_id == cid
        assert intents[1].task_type == "test"
        assert intents[1].baseline_cost_usd > 0
        assert intents[1].candidate_cost_usd == 0.0

        # Backward-compatible task-delegated.v1 event (Task 12)
        from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
            ModelTaskDelegatedEvent,
        )

        assert isinstance(intents[2], ModelTaskDelegatedEvent)
        assert intents[2].correlation_id == cid
        assert intents[2].quality_gate_passed is True
        assert intents[2].llm_call_id == "chatcmpl-abc123"
        from omnibase_infra.event_bus.topic_constants import (
            TOPIC_DELEGATION_TASK_DELEGATED,
        )

        assert intents[2].topic == TOPIC_DELEGATION_TASK_DELEGATED

    def test_completed_result_has_positive_latency(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()
        request = _make_request(correlation_id=cid)

        handler.handle_delegation_request(request)
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(_make_inference_response(correlation_id=cid))
        intents = handler.handle_gate_result(_make_gate_result(cid, passed=True))

        assert isinstance(intents[0], ModelDelegationEvent)
        result: ModelDelegationResult = intents[0].payload
        assert result.latency_ms >= 0


# ---------------------------------------------------------------------------
# Tests: Gate Failure
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGateFailure:
    """Test gate fail: request -> route -> infer -> gate fail -> failed event."""

    def test_gate_fail_produces_failed_event(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(
                correlation_id=cid,
                content="I'm sorry, I cannot help with that.",
            )
        )

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.2,
            failure_reasons=("REFUSAL: detected refusal phrases: i'm sorry",),
            fallback_recommended=True,
        )
        intents = handler.handle_gate_result(gate)

        # 2 events: delegation-failed + backward-compat task-delegated (no baseline on failure)
        assert len(intents) == 2
        assert isinstance(intents[0], ModelDelegationEvent)
        assert intents[0].topic == "onex.evt.omnibase-infra.delegation-failed.v1"
        assert handler.workflows[cid].state == EnumDelegationState.FAILED

        result: ModelDelegationResult = intents[0].payload
        assert result.quality_passed is False
        assert result.fallback_to_claude is True
        assert "REFUSAL" in result.failure_reason

        # Backward-compat event still emitted on failure
        from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
            ModelTaskDelegatedEvent,
        )

        assert isinstance(intents[1], ModelTaskDelegatedEvent)
        assert intents[1].quality_gate_passed is False

    def test_gate_fail_without_fallback(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(
                correlation_id=cid,
                content="some short content",
            )
        )

        gate = _make_gate_result(
            cid,
            passed=False,
            quality_score=0.5,
            failure_reasons=("TASK_MISMATCH: missing markers",),
            fallback_recommended=False,
        )
        intents = handler.handle_gate_result(gate)

        assert isinstance(intents[0], ModelDelegationEvent)
        result: ModelDelegationResult = intents[0].payload
        assert result.fallback_to_claude is False
        assert result.quality_passed is False


# ---------------------------------------------------------------------------
# Tests: Idempotency / Duplicate Events
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIdempotency:
    """Test duplicate event handling: same correlation_id twice -> no double processing."""

    def test_duplicate_request_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()
        request = _make_request(correlation_id=cid)

        intents1 = handler.handle_delegation_request(request)
        assert len(intents1) == 1

        intents2 = handler.handle_delegation_request(request)
        assert len(intents2) == 0

    def test_duplicate_routing_decision_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        decision = _make_routing_decision(cid)

        intents1 = handler.handle_routing_decision(decision)
        assert len(intents1) == 1

        intents2 = handler.handle_routing_decision(decision)
        assert len(intents2) == 0

    def test_duplicate_inference_response_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))

        intents1 = handler.handle_inference_response(
            _make_inference_response(correlation_id=cid, content="test content")
        )
        assert len(intents1) == 1

        intents2 = handler.handle_inference_response(
            _make_inference_response(correlation_id=cid, content="test content again")
        )
        assert len(intents2) == 0

    def test_request_after_completion_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(
                correlation_id=cid, content="def test_x():\n    pass"
            )
        )
        handler.handle_gate_result(_make_gate_result(cid, passed=True))

        assert handler.workflows[cid].state == EnumDelegationState.COMPLETED
        intents = handler.handle_delegation_request(_make_request(correlation_id=cid))
        assert len(intents) == 0


# ---------------------------------------------------------------------------
# Tests: Out-of-Order Events
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestOutOfOrder:
    """Test out-of-order events: events for wrong state are held/ignored."""

    def test_gate_result_before_inference_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))

        # Gate result arrives before inference (out of order)
        gate = _make_gate_result(cid, passed=True)
        intents = handler.handle_gate_result(gate)
        assert len(intents) == 0
        assert handler.workflows[cid].state == EnumDelegationState.ROUTED

    def test_inference_before_routing_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))

        # Inference arrives before routing decision
        intents = handler.handle_inference_response(
            _make_inference_response(correlation_id=cid, content="content")
        )
        assert len(intents) == 0
        assert handler.workflows[cid].state == EnumDelegationState.RECEIVED

    def test_routing_for_unknown_correlation_id_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        decision = _make_routing_decision(uuid4())

        intents = handler.handle_routing_decision(decision)
        assert len(intents) == 0

    def test_gate_for_unknown_correlation_id_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()
        gate = _make_gate_result(uuid4(), passed=True)

        intents = handler.handle_gate_result(gate)
        assert len(intents) == 0

    def test_inference_for_unknown_correlation_id_is_ignored(self) -> None:
        handler = HandlerDelegationWorkflow()

        intents = handler.handle_inference_response(
            _make_inference_response(correlation_id=uuid4(), content="content")
        )
        assert len(intents) == 0


# ---------------------------------------------------------------------------
# Tests: FSM State Transitions
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestFSMTransitions:
    """Test FSM state transition enforcement."""

    def test_valid_transition_received_to_routed(self) -> None:
        handler = HandlerDelegationWorkflow()
        workflow = DelegationWorkflowState(correlation_id=uuid4())
        handler._transition(workflow, EnumDelegationState.ROUTED)
        assert workflow.state == EnumDelegationState.ROUTED

    def test_invalid_transition_received_to_completed(self) -> None:
        handler = HandlerDelegationWorkflow()
        workflow = DelegationWorkflowState(correlation_id=uuid4())
        with pytest.raises(
            InvalidStateTransitionError, match="Invalid state transition"
        ):
            handler._transition(workflow, EnumDelegationState.COMPLETED)

    def test_terminal_state_cannot_transition(self) -> None:
        handler = HandlerDelegationWorkflow()
        workflow = DelegationWorkflowState(
            correlation_id=uuid4(), state=EnumDelegationState.COMPLETED
        )
        with pytest.raises(InvalidStateTransitionError):
            handler._transition(workflow, EnumDelegationState.RECEIVED)

    def test_failed_is_terminal(self) -> None:
        handler = HandlerDelegationWorkflow()
        workflow = DelegationWorkflowState(
            correlation_id=uuid4(), state=EnumDelegationState.FAILED
        )
        with pytest.raises(InvalidStateTransitionError):
            handler._transition(workflow, EnumDelegationState.RECEIVED)


# ---------------------------------------------------------------------------
# Tests: Multiple Concurrent Workflows
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestConcurrentWorkflows:
    """Test multiple workflows with different correlation_ids running concurrently."""

    def test_two_independent_workflows(self) -> None:
        handler = HandlerDelegationWorkflow()
        cid1 = uuid4()
        cid2 = uuid4()

        # Start both
        handler.handle_delegation_request(_make_request(correlation_id=cid1))
        handler.handle_delegation_request(
            _make_request(correlation_id=cid2, task_type="document")
        )

        # Route cid1 only
        handler.handle_routing_decision(_make_routing_decision(cid1))
        assert handler.workflows[cid1].state == EnumDelegationState.ROUTED
        assert handler.workflows[cid2].state == EnumDelegationState.RECEIVED

        # Complete cid2 through the full flow
        handler.handle_routing_decision(
            _make_routing_decision(cid2, task_type="document")
        )
        handler.handle_inference_response(
            _make_inference_response(
                correlation_id=cid2,
                content='"""Docstring."""',
                model_used="DeepSeek",
            )
        )
        handler.handle_gate_result(_make_gate_result(cid2, passed=True))

        assert handler.workflows[cid2].state == EnumDelegationState.COMPLETED
        assert handler.workflows[cid1].state == EnumDelegationState.ROUTED


# ---------------------------------------------------------------------------
# Tests: Topic routing — ModelTaskDelegatedEvent carries topic field
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTaskDelegatedEventTopicRouting:
    """Verify ModelTaskDelegatedEvent carries the correct topic field.

    The runtime kernel routes output_events by inspecting event.topic.
    Without it, the compat event is silently dropped.
    """

    def test_compat_event_has_topic_on_gate_pass(self) -> None:
        from omnibase_infra.event_bus.topic_constants import (
            TOPIC_DELEGATION_TASK_DELEGATED,
        )

        from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
            ModelTaskDelegatedEvent,
        )

        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(correlation_id=cid, content="def test_x(): pass")
        )
        events = handler.handle_gate_result(_make_gate_result(cid, passed=True))

        compat_events = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
        assert len(compat_events) == 1
        assert compat_events[0].topic == TOPIC_DELEGATION_TASK_DELEGATED

    def test_compat_event_has_topic_on_gate_fail(self) -> None:
        from omnibase_infra.event_bus.topic_constants import (
            TOPIC_DELEGATION_TASK_DELEGATED,
        )

        from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
            ModelTaskDelegatedEvent,
        )

        handler = HandlerDelegationWorkflow()
        cid = uuid4()

        handler.handle_delegation_request(_make_request(correlation_id=cid))
        handler.handle_routing_decision(_make_routing_decision(cid))
        handler.handle_inference_response(
            _make_inference_response(correlation_id=cid, content="I cannot help.")
        )
        events = handler.handle_gate_result(
            _make_gate_result(cid, passed=False, failure_reasons=("REFUSAL",))
        )

        compat_events = [e for e in events if isinstance(e, ModelTaskDelegatedEvent)]
        assert len(compat_events) == 1
        assert compat_events[0].topic == TOPIC_DELEGATION_TASK_DELEGATED

    def test_model_task_delegated_event_default_topic(self) -> None:
        from omnibase_infra.event_bus.topic_constants import (
            TOPIC_DELEGATION_TASK_DELEGATED,
        )

        from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
            ModelTaskDelegatedEvent,
        )

        event = ModelTaskDelegatedEvent(
            timestamp="2026-04-12T00:00:00Z",
            correlation_id=uuid4(),
            task_type="test",
            delegated_to="qwen3-coder",
            quality_gate_passed=True,
        )
        assert event.topic == TOPIC_DELEGATION_TASK_DELEGATED


# ---------------------------------------------------------------------------
# Tests: Enum / Model basics
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestEnumDelegationState:
    """Test the delegation state enum."""

    def test_all_states_present(self) -> None:
        states = {s.value for s in EnumDelegationState}
        assert states == {
            "RECEIVED",
            "ROUTED",
            "EXECUTING",
            "INFERENCE_COMPLETED",
            "GATE_EVALUATED",
            "COMPLETED",
            "FAILED",
        }

    def test_str_enum(self) -> None:
        assert str(EnumDelegationState.RECEIVED) == "RECEIVED"
