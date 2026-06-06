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

import hashlib
import json
import logging
import time
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import ClassVar
from uuid import UUID, uuid4

from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
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
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_escalation_attempt import (
    ModelDelegationEscalationAttempt,
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
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    next_eligible_tier,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.pricing import estimate_baseline_cost_usd, get_manifest_version_int

# Max tier escalation attempts for infra errors (auth, timeout, connection refused).
# Kept separate from handle_gate_result's max_escalation_attempts so callers can
# tune them independently.
_MAX_INFERENCE_ESCALATION_ATTEMPTS: int = 2
_INFERENCE_ERROR_EXCLUDED_TIERS: frozenset[str] = frozenset({"cli_agents"})

# Temperature by task type (Task 10, OMN-7040)
_TASK_TEMPERATURE: dict[str, float] = {
    "test": 0.3,
    "document": 0.5,
    "research": 0.7,
}

_logger = logging.getLogger(__name__)


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
            EnumDelegationState.ESCALATING,  # infra error on current tier → try next
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
        {
            EnumDelegationState.ESCALATING,
            EnumDelegationState.COMPLETED,
            EnumDelegationState.FAILED,
        }
    ),
    EnumDelegationState.ESCALATING: frozenset({EnumDelegationState.ROUTED}),
    EnumDelegationState.COMPLETED: frozenset(),
    EnumDelegationState.FAILED: frozenset(),
}


def _record_inference_response(
    workflow: DelegationWorkflowState,
    response: ModelInferenceResponseData,
) -> None:
    """Persist a single inference attempt's data onto the workflow."""
    workflow.inference_intent_in_flight = False
    workflow.inference_content = response.content
    workflow.inference_model_used = response.model_used
    workflow.inference_latency_ms = response.latency_ms
    workflow.inference_prompt_tokens = response.prompt_tokens
    workflow.inference_completion_tokens = response.completion_tokens
    workflow.inference_total_tokens = response.total_tokens
    workflow.inference_llm_call_id = response.llm_call_id


def _inference_timeout_seconds(workflow: DelegationWorkflowState) -> float:
    """Return the selected backend timeout in seconds, within wire-model bounds."""
    if workflow.routing_decision is None:
        return 30.0
    return max(1.0, min(600.0, workflow.routing_decision.timeout_ms / 1000.0))


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
                    dod_deterministic=workflow.routing_decision.dod_deterministic,
                    dod_heuristic=workflow.routing_decision.dod_heuristic,
                    quality_contract_mode=workflow.request.quality_contract_mode,
                    acceptance_criteria=workflow.request.acceptance_criteria,
                )
            )
        ]

    # Non-compliant, budget allows another attempt — emit repair prompt.
    # ROUTED -> ROUTED self-loop: stay in ROUTED, increment attempt counter.
    transition(workflow, EnumDelegationState.ROUTED)
    workflow.compliance_attempts += 1
    workflow.inference_intent_in_flight = True
    temperature = _TASK_TEMPERATURE.get(workflow.request.task_type, 0.3)
    return [
        ModelInferenceIntent(
            base_url=workflow.routing_decision.endpoint_url,
            model=workflow.routing_decision.selected_model,
            system_prompt=workflow.routing_decision.system_prompt,
            prompt=result.repair_prompt,
            max_tokens=workflow.request.max_tokens,
            temperature=temperature,
            timeout_seconds=_inference_timeout_seconds(workflow),
            correlation_id=workflow.correlation_id,
            api_key_ref=workflow.routing_decision.api_key_ref,
            extra_headers=workflow.routing_decision.extra_headers,
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
    inference_intent_in_flight: bool = False
    routing_intent_replayed: bool = False
    # Escalation state (OMN-12254). Tracks tier escalation across quality gate
    # failures. ``escalation_count`` is incremented on each escalation;
    # ``current_tier_name`` is set from ModelRoutingDecision.tier_name;
    # ``escalation_history`` records each tier attempt as a typed model.
    escalation_count: int = 0
    current_tier_name: str | None = None
    escalation_history: list[ModelDelegationEscalationAttempt] = field(
        default_factory=list
    )


class HandlerDelegationWorkflow:
    """Delegation orchestrator with correlation_id-keyed FSM state machine.

    Each delegation request creates a workflow keyed by its correlation_id.
    Events are matched to workflows by correlation_id and processed through
    the FSM. Duplicate or out-of-order events are handled safely.
    """

    _shared_workflows: ClassVar[dict[UUID, DelegationWorkflowState]] = {}

    def __init__(
        self,
        workflows: MutableMapping[UUID, DelegationWorkflowState] | None = None,
    ) -> None:
        self._workflows = workflows if workflows is not None else self._shared_workflows

    @property
    def workflows(self) -> MutableMapping[UUID, DelegationWorkflowState]:
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
            workflow = self._workflows[cid]
            if (
                workflow.state == EnumDelegationState.RECEIVED
                and workflow.routing_decision is None
                and not workflow.routing_intent_replayed
            ):
                workflow.routing_intent_replayed = True
                return [ModelRoutingIntent(payload=workflow.request or request)]
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
            _logger.warning(
                "Ignoring routing decision without active delegation workflow "
                "(correlation_id=%s)",
                cid,
            )
            return []

        if workflow.state == EnumDelegationState.RECEIVED:
            self._transition(workflow, EnumDelegationState.ROUTED)
            workflow.routing_decision = decision
            workflow.current_tier_name = decision.tier_name or None
            workflow.compliance_attempts = 1
        elif (
            workflow.state == EnumDelegationState.ROUTED
            and workflow.routing_decision is None
        ):
            # Escalation re-entry: ESCALATING -> ROUTED with a new routing decision.
            workflow.routing_decision = decision
            workflow.current_tier_name = decision.tier_name or None
            workflow.compliance_attempts += 1
        elif (
            workflow.state == EnumDelegationState.ROUTED
            and workflow.routing_decision is not None
            and workflow.inference_content is None
        ):
            if workflow.inference_intent_in_flight:
                return []
        else:
            return []

        if workflow.inference_intent_in_flight:
            return []

        workflow.inference_intent_in_flight = True

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
                timeout_seconds=_inference_timeout_seconds(workflow),
                correlation_id=cid,
                api_key_ref=decision.api_key_ref,
                extra_headers=decision.extra_headers,
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
        if workflow.routing_decision is None:
            return []

        if response.error_message:
            elapsed_ms = (time.monotonic_ns() - workflow.started_at_ns) // 1_000_000
            model_used = response.model_used or workflow.routing_decision.selected_model

            # Record this tier's failed attempt in escalation history before
            # deciding whether to escalate or terminate.
            workflow.escalation_history.append(
                ModelDelegationEscalationAttempt(
                    tier_name=workflow.current_tier_name or "unknown",
                    model_used=model_used,
                    quality_score=0.0,
                    failure_reasons=(response.error_message,),
                    latency_ms=elapsed_ms,
                    fallback_recommended=True,
                    attempted_at=datetime.now(UTC),
                    routing_decision_id=workflow.routing_decision.selected_backend_id,
                )
            )

            # Infra errors (auth failure, connection refused, timeout) are
            # retryable at the next tier — attempt escalation before terminal FAILED.
            terminal_failure_reason: str | None = None
            next_tier: str | None = None

            if workflow.escalation_count >= _MAX_INFERENCE_ESCALATION_ATTEMPTS:
                terminal_failure_reason = "max_escalation_attempts_reached"
            elif workflow.current_tier_name is None:
                terminal_failure_reason = "current_tier_unknown"
            else:
                next_tier = next_eligible_tier(
                    workflow.current_tier_name,
                    _INFERENCE_ERROR_EXCLUDED_TIERS,
                )
                if next_tier is None:
                    terminal_failure_reason = "no_higher_tier_available"

            can_escalate = terminal_failure_reason is None and next_tier is not None

            if can_escalate:
                assert next_tier is not None
                self._transition(workflow, EnumDelegationState.ESCALATING)
                workflow.escalation_count += 1

                workflow.inference_content = None
                workflow.inference_model_used = None
                workflow.inference_intent_in_flight = False
                workflow.routing_decision = None

                self._transition(workflow, EnumDelegationState.ROUTED)
                assert workflow.request is not None
                return [
                    ModelRoutingIntent(
                        payload=workflow.request,
                        min_tier_name=next_tier,
                    ),
                ]

            # No escalation possible: terminal FAILED.
            _record_inference_response(workflow, response)
            escalation_metadata = self._escalation_metadata(
                workflow,
                terminal_failure_reason=terminal_failure_reason,
            )
            delegation_result = ModelDelegationResult(
                correlation_id=response.correlation_id,
                task_type=workflow.request.task_type,
                model_used=model_used,
                endpoint_url=workflow.routing_decision.endpoint_url,
                content=response.content,
                quality_passed=False,
                quality_score=0.0,
                latency_ms=elapsed_ms,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                total_tokens=response.total_tokens,
                fallback_to_claude=False,
                failure_reason=response.error_message,
                tokens_to_compliance=workflow.accumulated_tokens,
                compliance_attempts=workflow.compliance_attempts or 1,
                **escalation_metadata,
            )
            compat_event = ModelTaskDelegatedEvent(
                topic=TOPIC_DELEGATION_TASK_DELEGATED,
                timestamp=datetime.now(UTC).isoformat(),
                correlation_id=response.correlation_id,
                session_id=None,
                task_type=workflow.request.task_type,
                delegated_to=model_used,
                model_name=workflow.routing_decision.selected_model,
                quality_gate_passed=False,
                quality_gates_failed=[response.error_message],
                cost_usd=0.0,
                cost_savings_usd=0.0,
                delegation_latency_ms=elapsed_ms,
                llm_call_id=response.llm_call_id,
                tokens_to_compliance=workflow.accumulated_tokens,
                compliance_attempts=workflow.compliance_attempts or 1,
                pricing_manifest_version=get_manifest_version_int(),
                escalation_count=workflow.escalation_count,
                escalation_history=tuple(
                    attempt.model_dump(mode="json")
                    for attempt in workflow.escalation_history
                ),
                routing_tiers_hash=self._routing_tiers_hash(),
                attempts_count=workflow.escalation_count + 1,
            )
            self._transition(workflow, EnumDelegationState.FAILED)
            return [
                ModelDelegationEvent(
                    topic=TOPIC_DELEGATION_FAILED,
                    payload=delegation_result,
                ),
                compat_event,
            ]

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
                        dod_deterministic=workflow.routing_decision.dod_deterministic,
                        dod_heuristic=workflow.routing_decision.dod_heuristic,
                        quality_contract_mode=workflow.request.quality_contract_mode,
                        acceptance_criteria=workflow.request.acceptance_criteria,
                    )
                )
            ]

        # Compliance-loop path.
        return _evaluate_compliance(workflow, response, self._transition)

    def handle_gate_result(
        self,
        result: ModelQualityGateResult,
        *,
        max_escalation_attempts: int = 2,
        excluded_tiers: frozenset[str] = frozenset({"cli_agents"}),
    ) -> list[BaseModel]:
        """Handle quality gate result with escalation support (OMN-12254).

        Transitions INFERENCE_COMPLETED -> GATE_EVALUATED, then evaluates:
        - Passed -> COMPLETED (unchanged)
        - Failed + escalation possible -> ESCALATING -> ROUTED (new)
        - Failed + escalation impossible -> FAILED (with terminal_failure_reason)

        Returns:
        1. The delegation result event (completed or failed)
        2. A backward-compatible task-delegated.v1 event for omnidash
        3. A baseline comparison intent for savings computation (pass only)
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

        events: list[BaseModel] = []

        if result.passed:
            # --- PASSED: complete as before ---
            escalation_metadata = self._escalation_metadata(workflow)
            delegation_result = ModelDelegationResult(
                correlation_id=cid,
                task_type=workflow.request.task_type,
                model_used=workflow.inference_model_used,
                endpoint_url=workflow.routing_decision.endpoint_url,
                content=workflow.inference_content,
                quality_passed=True,
                quality_score=result.quality_score,
                latency_ms=elapsed_ms,
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
                total_tokens=workflow.inference_total_tokens,
                fallback_to_claude=result.fallback_recommended,
                failure_reason="",
                tokens_to_compliance=tokens_to_compliance,
                compliance_attempts=compliance_attempts,
                **escalation_metadata,
            )

            estimated_claude_cost = estimate_baseline_cost_usd(
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
            )

            compat_event = self._build_compat_event(
                workflow,
                result,
                elapsed_ms,
                tokens_to_compliance,
                compliance_attempts,
            )

            self._transition(workflow, EnumDelegationState.COMPLETED)
            events.append(
                ModelDelegationEvent(
                    topic=TOPIC_DELEGATION_COMPLETED,
                    payload=delegation_result,
                )
            )
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
            events.append(compat_event)
            return events

        # --- FAILED: evaluate escalation (OMN-12254) ---

        # Record this tier attempt in escalation history.
        workflow.escalation_history.append(
            ModelDelegationEscalationAttempt(
                tier_name=workflow.current_tier_name or "unknown",
                model_used=workflow.inference_model_used or "unknown",
                quality_score=result.quality_score,
                failure_reasons=tuple(result.failure_reasons),
                latency_ms=elapsed_ms,
                fallback_recommended=result.fallback_recommended,
                attempted_at=result.evaluated_at
                if hasattr(result, "evaluated_at") and result.evaluated_at is not None
                else datetime.now(UTC),
                routing_decision_id=workflow.routing_decision.selected_backend_id,
            )
        )

        # Determine why escalation cannot proceed (for audit trail).
        terminal_failure_reason: str | None = None
        next_tier: str | None = None

        if not result.fallback_recommended:
            terminal_failure_reason = "fallback_not_recommended"
        elif workflow.escalation_count >= max_escalation_attempts:
            terminal_failure_reason = "max_escalation_attempts_reached"
        elif workflow.current_tier_name is None:
            terminal_failure_reason = "current_tier_unknown"
        else:
            next_tier = next_eligible_tier(
                workflow.current_tier_name,
                excluded_tiers,
            )
            if next_tier is None:
                terminal_failure_reason = "no_higher_tier_available"

        can_escalate = terminal_failure_reason is None and next_tier is not None

        if can_escalate:
            assert next_tier is not None
            self._transition(workflow, EnumDelegationState.ESCALATING)
            workflow.escalation_count += 1

            # Reset inference state for the new attempt.
            workflow.inference_content = None
            workflow.inference_model_used = None
            workflow.inference_intent_in_flight = False
            workflow.routing_decision = None

            # Transition to ROUTED and emit new routing intent with tier override.
            self._transition(workflow, EnumDelegationState.ROUTED)
            assert workflow.request is not None
            return [
                ModelRoutingIntent(
                    payload=workflow.request,
                    min_tier_name=next_tier,
                ),
            ]

        # Cannot escalate: terminal FAILED with reason.
        escalation_metadata = self._escalation_metadata(
            workflow,
            terminal_failure_reason=terminal_failure_reason,
        )
        delegation_result = ModelDelegationResult(
            correlation_id=cid,
            task_type=workflow.request.task_type,
            model_used=workflow.inference_model_used,
            endpoint_url=workflow.routing_decision.endpoint_url,
            content=workflow.inference_content,
            quality_passed=False,
            quality_score=result.quality_score,
            latency_ms=elapsed_ms,
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
            total_tokens=workflow.inference_total_tokens,
            fallback_to_claude=result.fallback_recommended,
            failure_reason="; ".join(result.failure_reasons),
            tokens_to_compliance=tokens_to_compliance,
            compliance_attempts=compliance_attempts,
            **escalation_metadata,
        )

        compat_event = self._build_compat_event(
            workflow,
            result,
            elapsed_ms,
            tokens_to_compliance,
            compliance_attempts,
        )

        self._transition(workflow, EnumDelegationState.FAILED)
        events.append(
            ModelDelegationEvent(
                topic=TOPIC_DELEGATION_FAILED,
                payload=delegation_result,
            )
        )
        events.append(compat_event)
        return events

    def _escalation_metadata(
        self,
        workflow: DelegationWorkflowState,
        *,
        terminal_failure_reason: str | None = None,
    ) -> dict[str, object]:
        """Build escalation metadata fields for terminal events."""
        history_dicts = tuple(
            attempt.model_dump(mode="json") for attempt in workflow.escalation_history
        )
        return {
            "escalation_count": workflow.escalation_count,
            "escalation_history": history_dicts,
            "terminal_failure_reason": terminal_failure_reason,
            "routing_tiers_hash": self._routing_tiers_hash(),
            "escalation_config_hash": None,
            "attempts_count": workflow.escalation_count + 1,
            "cumulative_attempt_cost": 0.0,
            "cumulative_input_tokens": 0,
            "cumulative_output_tokens": 0,
            "final_attempt_cost": 0.0,
        }

    @staticmethod
    def _routing_tiers_hash() -> str | None:
        """SHA-256 of routing_tiers.yaml for replay determinism."""
        from pathlib import Path

        config_path = (
            Path(__file__).parent.parent.parent.parent.parent
            / "configs"
            / "routing_tiers.yaml"
        )
        if not config_path.exists():
            return None
        content = config_path.read_bytes()
        return hashlib.sha256(content).hexdigest()

    def _build_compat_event(
        self,
        workflow: DelegationWorkflowState,
        result: ModelQualityGateResult,
        elapsed_ms: int,
        tokens_to_compliance: int,
        compliance_attempts: int,
    ) -> ModelTaskDelegatedEvent:
        """Build backward-compatible task-delegated.v1 event for omnidash."""
        assert workflow.request is not None
        assert workflow.routing_decision is not None
        assert workflow.inference_model_used is not None

        estimated_claude_cost = estimate_baseline_cost_usd(
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
        )

        history_dicts = tuple(
            attempt.model_dump(mode="json") for attempt in workflow.escalation_history
        )

        return ModelTaskDelegatedEvent(
            topic=TOPIC_DELEGATION_TASK_DELEGATED,
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=result.correlation_id,
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
            pricing_manifest_version=get_manifest_version_int(),
            escalation_count=workflow.escalation_count,
            escalation_history=history_dicts,
            routing_tiers_hash=self._routing_tiers_hash(),
            attempts_count=workflow.escalation_count + 1,
        )

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

    async def handle(self, payload: object) -> list[BaseModel]:
        """Route supported workflow payloads through the canonical FSM methods."""
        if isinstance(payload, ModelEventEnvelope) or hasattr(payload, "payload"):
            payload = payload.payload
        if isinstance(payload, dict):
            payload = self._coerce_payload_dict(payload)
        if isinstance(payload, ModelDelegationRequest):
            return list(self.handle_delegation_request(payload))
        if isinstance(payload, ModelInvocationCommand):
            return list(self.handle_invocation_command(payload))
        if isinstance(payload, ModelRoutingDecision):
            return list(self.handle_routing_decision(payload))
        if isinstance(payload, ModelInferenceResponseData):
            return list(self.handle_inference_response(payload))
        if isinstance(payload, ModelQualityGateResult):
            return list(self.handle_gate_result(payload))
        if isinstance(payload, ModelAgentTaskLifecycleEvent):
            return list(self.handle_agent_task_lifecycle(payload))
        msg = f"Unsupported delegation workflow payload: {type(payload).__name__}"
        raise ValueError(msg)

    async def handle_async(self, payload: object) -> ModelHandlerOutput[None]:
        """Runtime auto-wiring entrypoint that returns publishable handler output."""
        events = await self.handle(payload)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=self._coerce_payload_correlation_id(payload),
            handler_id="node_delegation_orchestrator.workflow",
            events=tuple(events),
        )

    @staticmethod
    def _coerce_payload_correlation_id(payload: object) -> UUID:
        candidate = getattr(payload, "correlation_id", None)
        if candidate is None and isinstance(payload, ModelEventEnvelope):
            candidate = payload.correlation_id
        if candidate is None and hasattr(payload, "payload"):
            candidate = getattr(payload.payload, "correlation_id", None)
        if candidate is None and isinstance(payload, dict):
            nested_payload = payload.get("payload")
            candidate = payload.get("correlation_id")
            if candidate is None and isinstance(nested_payload, dict):
                candidate = nested_payload.get("correlation_id")
        if isinstance(candidate, UUID):
            return candidate
        if isinstance(candidate, str) and candidate:
            return UUID(candidate)
        return uuid4()

    @staticmethod
    def _coerce_payload_dict(payload: dict[str, object]) -> BaseModel:
        """Convert raw event-bus payload dictionaries into workflow models."""
        nested_payload = payload.get("payload")
        if isinstance(nested_payload, dict) and (
            "event_type" in payload or "envelope_id" in payload
        ):
            return HandlerDelegationWorkflow._coerce_payload_dict(nested_payload)
        if "lifecycle_type" in payload:
            return ModelAgentTaskLifecycleEvent.model_validate(payload)
        if "invocation_kind" in payload or "target_ref" in payload:
            return ModelInvocationCommand.model_validate(payload)
        if "selected_model" in payload or "selected_backend_id" in payload:
            return ModelRoutingDecision.model_validate(payload)
        if "model_used" in payload or "llm_call_id" in payload:
            return ModelInferenceResponseData.model_validate(payload)
        if "quality_score" in payload or "passed" in payload:
            return ModelQualityGateResult.model_validate(payload)
        if "prompt" in payload and "task_type" in payload:
            return ModelDelegationRequest.model_validate(payload)
        msg = "Unsupported delegation workflow payload dictionary"
        raise ValueError(msg)

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
