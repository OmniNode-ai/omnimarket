# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# Copyright (c) 2026 OmniNode Team
"""Delegation orchestrator handler with correlation_id-keyed FSM.

Coordinates the full delegation workflow:
1. Receive ModelDelegationRequest -> state RECEIVED
2. Invoke routing reducer -> state ROUTED
3. Invoke LLM inference effect -> state INFERENCE_COMPLETED
4. Invoke quality gate reducer -> state GATE_EVALUATED
5. Emit delegation-completed or delegation-failed -> COMPLETED | FAILED

The FSM is replay-safe: duplicate events for the same correlation_id
are rejected if the workflow is already in or past that state.

Related:
    - OMN-7040: Node-based delegation pipeline
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_infra.event_bus.topic_constants import (
    TOPIC_DELEGATION_COMPLETED,
    TOPIC_DELEGATION_FAILED,
    TOPIC_DELEGATION_TASK_DELEGATED,
)
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.enums import (
    EnumDelegationState,
)
from omnimarket.nodes.node_delegation_orchestrator.lifecycle_reactor import (
    next_state_from_lifecycle,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_baseline_intent import (
    ModelBaselineIntent,
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
from omnimarket.nodes.node_delegation_orchestrator.models.model_task_delegated_event import (
    ModelTaskDelegatedEvent,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)

# Temperature by task type (Task 10, OMN-7040)
_TASK_TEMPERATURE: dict[str, float] = {
    "test": 0.3,
    "document": 0.5,
    "research": 0.7,
}

# Approximate Claude pricing for savings estimation (Task 11, OMN-7040)
# Claude Sonnet 3.5: ~$3/M input, ~$15/M output tokens
_CLAUDE_INPUT_PRICE_PER_TOKEN: float = 3.0 / 1_000_000
_CLAUDE_OUTPUT_PRICE_PER_TOKEN: float = 15.0 / 1_000_000

# Valid state transitions: from_state -> set of valid to_states
# Note: ROUTED -> ROUTED self-loop (OMN-10794) supports the schema-compliance
# loop's repair re-prompts — when an inference response fails validation and
# the budget allows another attempt, the orchestrator stays in ROUTED while
# emitting a fresh ModelInferenceIntent carrying the repair prompt.
_VALID_TRANSITIONS: dict[EnumDelegationState, frozenset[EnumDelegationState]] = {
    EnumDelegationState.RECEIVED: frozenset({EnumDelegationState.ROUTED}),
    EnumDelegationState.ROUTED: frozenset(
        {
            EnumDelegationState.ROUTED,  # OMN-10794 — schema-repair re-prompt
            EnumDelegationState.EXECUTING,
            EnumDelegationState.INFERENCE_COMPLETED,
            EnumDelegationState.COMPLETED,
            EnumDelegationState.FAILED,
        }
    ),
    EnumDelegationState.EXECUTING: frozenset(
        {EnumDelegationState.COMPLETED, EnumDelegationState.FAILED}
    ),
    EnumDelegationState.INFERENCE_COMPLETED: frozenset(
        {EnumDelegationState.GATE_EVALUATED}
    ),
    EnumDelegationState.GATE_EVALUATED: frozenset(
        {EnumDelegationState.COMPLETED, EnumDelegationState.FAILED}
    ),
    EnumDelegationState.COMPLETED: frozenset(),
    EnumDelegationState.FAILED: frozenset(),
}


def _record_inference_response(
    workflow: DelegationWorkflowState,
    response: ModelInferenceResponseData,
) -> None:
    """Persist a single inference attempt's data onto the workflow."""
    workflow.inference_content = response.content
    workflow.inference_model_used = response.model_used
    workflow.inference_latency_ms = response.latency_ms
    workflow.inference_prompt_tokens = response.prompt_tokens
    workflow.inference_completion_tokens = response.completion_tokens
    workflow.inference_total_tokens = response.total_tokens
    workflow.inference_llm_call_id = response.llm_call_id


def _evaluate_compliance(
    workflow: DelegationWorkflowState,
    response: ModelInferenceResponseData,
    transition: Callable[[DelegationWorkflowState, EnumDelegationState], None],
) -> list[BaseModel]:
    """Run one compliance-loop iteration; emit repair intent or accept (OMN-10794).

    Pre: workflow.state == ROUTED and workflow.request.output_schema_key
    is not None and workflow.request.compliance_budget is not None.
    """
    # Local import to keep the cold path off the legacy boot path.
    from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_compliance_loop import (
        HandlerComplianceLoop,
    )

    assert workflow.request is not None
    assert workflow.request.output_schema_key is not None
    assert workflow.request.compliance_budget is not None
    assert workflow.routing_decision is not None

    loop = HandlerComplianceLoop()
    result = loop.evaluate(
        candidate_output=response.content,
        schema_key=workflow.request.output_schema_key,
        original_prompt=workflow.request.prompt,
        attempt_number=workflow.compliance_attempts,
        cumulative_tokens=workflow.accumulated_tokens,
        attempt_tokens=response.total_tokens,
        budget_limits=workflow.request.compliance_budget,
        run_id=str(workflow.correlation_id),
    )

    # Always update the running token total.
    workflow.accumulated_tokens = result.tokens_to_compliance

    if result.compliant or result.repair_prompt == "":
        # Compliant or budget ABORT — record this attempt and forward to gate.
        transition(workflow, EnumDelegationState.INFERENCE_COMPLETED)
        _record_inference_response(workflow, response)
        return [
            ModelQualityGateIntent(
                payload=ModelQualityGateInput(
                    correlation_id=response.correlation_id,
                    task_type=workflow.request.task_type,
                    llm_response_content=response.content,
                )
            )
        ]

    # Non-compliant, budget allows another attempt — emit repair prompt.
    # ROUTED -> ROUTED self-loop: stay in ROUTED, increment attempt counter.
    transition(workflow, EnumDelegationState.ROUTED)
    workflow.compliance_attempts += 1
    temperature = _TASK_TEMPERATURE.get(workflow.request.task_type, 0.3)
    return [
        ModelInferenceIntent(
            base_url=workflow.routing_decision.endpoint_url,
            model=workflow.routing_decision.selected_model,
            system_prompt=workflow.routing_decision.system_prompt,
            prompt=result.repair_prompt,
            max_tokens=workflow.request.max_tokens,
            temperature=temperature,
            correlation_id=workflow.correlation_id,
        )
    ]


@dataclass
class DelegationWorkflowState:
    """Mutable workflow state for a single delegation correlation_id."""

    correlation_id: UUID
    state: EnumDelegationState = EnumDelegationState.RECEIVED
    request: ModelDelegationRequest | None = None
    routing_decision: ModelRoutingDecision | None = None
    invocation_command: ModelInvocationCommand | None = None
    inference_content: str | None = None
    inference_model_used: str | None = None
    inference_latency_ms: int = 0
    inference_prompt_tokens: int = 0
    inference_completion_tokens: int = 0
    inference_total_tokens: int = 0
    inference_llm_call_id: str = ""
    gate_result: ModelQualityGateResult | None = None
    started_at_ns: int = field(default_factory=time.monotonic_ns)
    # Compliance-loop counters (OMN-10794). The orchestrator owns the loop,
    # ``compliance_attempts`` counts the inference attempts it has issued so
    # far (1 = first attempt) and ``accumulated_tokens`` is the running sum
    # of tokens across all attempts. Both are forwarded onto the terminal
    # ModelDelegationResult / ModelTaskDelegatedEvent.
    compliance_attempts: int = 0
    accumulated_tokens: int = 0


class HandlerDelegationWorkflow:
    """Delegation orchestrator with correlation_id-keyed FSM state machine.

    Each delegation request creates a workflow keyed by its correlation_id.
    Events are matched to workflows by correlation_id and processed through
    the FSM. Duplicate or out-of-order events are handled safely.
    """

    def __init__(self) -> None:
        self._workflows: dict[UUID, DelegationWorkflowState] = {}

    @property
    def workflows(self) -> dict[UUID, DelegationWorkflowState]:
        """Expose workflows for testing/observability."""
        return self._workflows

    def _transition(
        self,
        workflow: DelegationWorkflowState,
        target: EnumDelegationState,
    ) -> None:
        """Transition workflow to target state, enforcing FSM validity."""
        valid = _VALID_TRANSITIONS.get(workflow.state, frozenset())
        if target not in valid:
            msg = (
                f"Invalid state transition: {workflow.state} -> {target} "
                f"for correlation_id={workflow.correlation_id}"
            )
            raise InvalidStateTransitionError(msg)
        workflow.state = target

    def handle_delegation_request(
        self,
        request: ModelDelegationRequest,
    ) -> list[ModelRoutingIntent]:
        """Handle incoming delegation request. Returns intents to emit.

        Creates a new workflow for this correlation_id or rejects duplicates.
        Emits an intent to the routing reducer.
        """
        cid = request.correlation_id

        if cid in self._workflows:
            return []

        workflow = DelegationWorkflowState(
            correlation_id=cid,
            request=request,
        )
        self._workflows[cid] = workflow

        return [ModelRoutingIntent(payload=request)]

    def handle_invocation_command(
        self,
        command: ModelInvocationCommand,
    ) -> list[ModelInvocationCommand]:
        """Handle typed invocation command from the routing reducer."""
        workflow = self._workflows.get(command.correlation_id)
        if workflow is None:
            return []
        if workflow.state != EnumDelegationState.RECEIVED:
            return []

        self._transition(workflow, EnumDelegationState.ROUTED)
        workflow.invocation_command = command
        return [command]

    def handle_routing_decision(
        self,
        decision: ModelRoutingDecision,
    ) -> list[ModelInferenceIntent]:
        """Handle routing decision from the routing reducer.

        Transitions RECEIVED -> ROUTED, then emits intent to LLM inference.
        This is attempt #1 of the compliance loop (OMN-10794).
        """
        cid = decision.correlation_id
        workflow = self._workflows.get(cid)
        if workflow is None:
            return []

        if workflow.state != EnumDelegationState.RECEIVED:
            return []

        self._transition(workflow, EnumDelegationState.ROUTED)
        workflow.routing_decision = decision
        workflow.compliance_attempts = 1

        assert workflow.request is not None
        temperature = _TASK_TEMPERATURE.get(workflow.request.task_type, 0.3)
        return [
            ModelInferenceIntent(
                base_url=decision.endpoint_url,
                model=decision.selected_model,
                system_prompt=decision.system_prompt,
                prompt=workflow.request.prompt,
                max_tokens=workflow.request.max_tokens,
                temperature=temperature,
                correlation_id=cid,
            )
        ]

    def handle_inference_response(
        self,
        response: ModelInferenceResponseData,
    ) -> list[BaseModel]:
        """Handle LLM inference response.

        Two paths:

        1. **Legacy** (request.output_schema_key is None) — accept the response
           on the first attempt and forward to the quality gate. Transitions
           ROUTED -> INFERENCE_COMPLETED.

        2. **Compliance loop** (request.output_schema_key is set, OMN-10794) —
           validate the response against the registered schema. On success or
           budget-abort, accumulate tokens and forward to the quality gate
           (ROUTED -> INFERENCE_COMPLETED). On non-compliant + budget CONTINUE,
           emit a fresh ModelInferenceIntent with the repair prompt and stay
           in ROUTED (self-loop).
        """
        workflow = self._workflows.get(response.correlation_id)
        if workflow is None:
            return []

        if workflow.state != EnumDelegationState.ROUTED:
            return []

        assert workflow.request is not None
        assert workflow.routing_decision is not None

        # Legacy path: no compliance loop, single attempt.
        if workflow.request.output_schema_key is None:
            self._transition(workflow, EnumDelegationState.INFERENCE_COMPLETED)
            _record_inference_response(workflow, response)
            workflow.accumulated_tokens = response.total_tokens
            return [
                ModelQualityGateIntent(
                    payload=ModelQualityGateInput(
                        correlation_id=response.correlation_id,
                        task_type=workflow.request.task_type,
                        llm_response_content=response.content,
                    )
                )
            ]

        # Compliance-loop path.
        return _evaluate_compliance(workflow, response, self._transition)

    def handle_gate_result(
        self,
        result: ModelQualityGateResult,
    ) -> list[BaseModel]:
        """Handle quality gate result.

        Transitions INFERENCE_COMPLETED -> GATE_EVALUATED, then evaluates
        pass/fail to transition to COMPLETED or FAILED. Returns:
        1. The delegation result event (completed or failed)
        2. A backward-compatible task-delegated.v1 event for omnidash (Task 12)
        3. A baseline comparison intent for savings computation (Task 11, pass only)
        """
        cid = result.correlation_id
        workflow = self._workflows.get(cid)
        if workflow is None:
            return []

        if workflow.state != EnumDelegationState.INFERENCE_COMPLETED:
            return []

        self._transition(workflow, EnumDelegationState.GATE_EVALUATED)
        workflow.gate_result = result

        assert workflow.request is not None
        assert workflow.routing_decision is not None
        assert workflow.inference_content is not None
        assert workflow.inference_model_used is not None

        elapsed_ms = (time.monotonic_ns() - workflow.started_at_ns) // 1_000_000

        # Compliance counters (OMN-10794): defaults preserve legacy single-attempt
        # semantics (1 attempt, total_tokens of that attempt) when the request
        # didn't opt into the compliance loop.
        compliance_attempts = workflow.compliance_attempts or 1
        tokens_to_compliance = (
            workflow.accumulated_tokens or workflow.inference_total_tokens
        )

        delegation_result = ModelDelegationResult(
            correlation_id=cid,
            task_type=workflow.request.task_type,
            model_used=workflow.inference_model_used,
            endpoint_url=workflow.routing_decision.endpoint_url,
            content=workflow.inference_content,
            quality_passed=result.passed,
            quality_score=result.quality_score,
            latency_ms=elapsed_ms,
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
            total_tokens=workflow.inference_total_tokens,
            fallback_to_claude=result.fallback_recommended,
            failure_reason="; ".join(result.failure_reasons)
            if not result.passed
            else "",
            tokens_to_compliance=tokens_to_compliance,
            compliance_attempts=compliance_attempts,
        )

        # Estimate Claude cost for savings comparison (Task 11)
        estimated_claude_cost = (
            workflow.inference_prompt_tokens * _CLAUDE_INPUT_PRICE_PER_TOKEN
            + workflow.inference_completion_tokens * _CLAUDE_OUTPUT_PRICE_PER_TOKEN
        )

        # Backward-compatible task-delegated.v1 event for omnidash (Task 12)
        compat_event = ModelTaskDelegatedEvent(
            topic=TOPIC_DELEGATION_TASK_DELEGATED,
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=cid,
            session_id=None,
            task_type=workflow.request.task_type,
            delegated_to=workflow.inference_model_used,
            model_name=workflow.routing_decision.selected_model,
            quality_gate_passed=result.passed,
            quality_gates_failed=list(result.failure_reasons),
            cost_usd=0.0,
            cost_savings_usd=round(estimated_claude_cost, 6),
            delegation_latency_ms=elapsed_ms,
            llm_call_id=workflow.inference_llm_call_id,
            tokens_to_compliance=tokens_to_compliance,
            compliance_attempts=compliance_attempts,
        )

        events: list[BaseModel] = []

        if result.passed:
            self._transition(workflow, EnumDelegationState.COMPLETED)
            events.append(
                ModelDelegationEvent(
                    topic=TOPIC_DELEGATION_COMPLETED,
                    payload=delegation_result,
                )
            )
            # Baseline comparison for savings pipeline (Task 11)
            events.append(
                ModelBaselineIntent(
                    correlation_id=cid,
                    task_type=workflow.request.task_type,
                    baseline_cost_usd=estimated_claude_cost,
                    candidate_cost_usd=0.0,
                    prompt_tokens=workflow.inference_prompt_tokens,
                    completion_tokens=workflow.inference_completion_tokens,
                    total_tokens=workflow.inference_total_tokens,
                )
            )
        else:
            self._transition(workflow, EnumDelegationState.FAILED)
            events.append(
                ModelDelegationEvent(
                    topic=TOPIC_DELEGATION_FAILED,
                    payload=delegation_result,
                )
            )

        # Always emit backward-compatible event for omnidash (Task 12)
        events.append(compat_event)

        return events

    def handle_agent_task_lifecycle(
        self,
        lifecycle_event: ModelAgentTaskLifecycleEvent,
    ) -> list[BaseModel]:
        """Handle remote-agent lifecycle events from the A2A effect lane."""
        cid = lifecycle_event.correlation_id
        workflow = self._workflows.get(cid)
        if workflow is None:
            return []

        next_state = next_state_from_lifecycle(lifecycle_event.lifecycle_type)
        if next_state is EnumDelegationState.EXECUTING:
            if workflow.state == EnumDelegationState.ROUTED:
                self._transition(workflow, EnumDelegationState.EXECUTING)
            return []

        if workflow.state not in {
            EnumDelegationState.ROUTED,
            EnumDelegationState.EXECUTING,
        }:
            return []

        if workflow.state != next_state:
            self._transition(workflow, next_state)

        assert workflow.request is not None

        elapsed_ms = (time.monotonic_ns() - workflow.started_at_ns) // 1_000_000
        delegated_to = (
            workflow.invocation_command.target_ref
            if workflow.invocation_command is not None
            else "remote-agent"
        )
        content = self._render_lifecycle_content(lifecycle_event)
        failure_reason = lifecycle_event.error or ""

        delegation_result = ModelDelegationResult(
            correlation_id=cid,
            task_type=workflow.request.task_type,
            model_used=delegated_to,
            endpoint_url=delegated_to,
            content=content,
            quality_passed=next_state is EnumDelegationState.COMPLETED,
            quality_score=1.0 if next_state is EnumDelegationState.COMPLETED else 0.0,
            latency_ms=elapsed_ms,
            fallback_to_claude=False,
            failure_reason=failure_reason,
        )

        compat_event = ModelTaskDelegatedEvent(
            topic=TOPIC_DELEGATION_TASK_DELEGATED,
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=cid,
            session_id=None,
            task_type=workflow.request.task_type,
            delegated_to=delegated_to,
            model_name=delegated_to,
            quality_gate_passed=next_state is EnumDelegationState.COMPLETED,
            quality_gates_checked=["agent-task-lifecycle"],
            quality_gates_failed=[failure_reason] if failure_reason else [],
            cost_usd=0.0,
            cost_savings_usd=0.0,
            delegation_latency_ms=elapsed_ms,
            llm_call_id=lifecycle_event.remote_task_handle or "",
        )

        topic = (
            TOPIC_DELEGATION_COMPLETED
            if next_state is EnumDelegationState.COMPLETED
            else TOPIC_DELEGATION_FAILED
        )
        return [
            ModelDelegationEvent(topic=topic, payload=delegation_result),
            compat_event,
        ]

    @staticmethod
    def _render_lifecycle_content(
        lifecycle_event: ModelAgentTaskLifecycleEvent,
    ) -> str:
        """Render lifecycle payload into the legacy content string field."""
        if lifecycle_event.artifact is not None:
            plain = {
                key: value.to_value() for key, value in lifecycle_event.artifact.items()
            }
            return json.dumps(plain, sort_keys=True)
        if lifecycle_event.error:
            return lifecycle_event.error
        return lifecycle_event.lifecycle_type.value


class InvalidStateTransitionError(Exception):
    """Raised when an FSM state transition is invalid."""


__all__: list[str] = [
    "DelegationWorkflowState",
    "HandlerDelegationWorkflow",
    "InvalidStateTransitionError",
]
