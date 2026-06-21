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
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID, uuid4

from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from pydantic import BaseModel

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.models.delegation.quality_bar_evidence import (
    format_quality_bar_labels,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_FAILED,
    TOPIC_ID_TASK_DELEGATED,
)
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
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    RequiredBarAuthority,
    RequiredBarAuthorityError,
    resolve_required_bar_authority,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_input import (
    ModelQualityGateInput,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    NO_HIGHER_TIER_REASON_TOKEN,
    describe_no_higher_tier_available,
    next_eligible_tier,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.pricing import (
    ModelActualCostMeasurement,
    build_premium_counterfactual,
    estimate_baseline_cost_usd,
    get_manifest_version_int,
    recompute_actual_cost_and_savings,
)

# Max tier escalation attempts for infra errors (auth, timeout, connection refused).
# Kept separate from handle_gate_result's max_escalation_attempts so callers can
# tune them independently.
_MAX_INFERENCE_ESCALATION_ATTEMPTS: int = 2
# OMN-13215: the shelled ``cli_agents`` tier was removed. Every tier — including
# the ceiling (claude) — now executes through the canonical HTTP inference path, so
# no tier is excluded from inference-error escalation.
_INFERENCE_ERROR_EXCLUDED_TIERS: frozenset[str] = frozenset()
# OMN-13140 GATE 1 classification. Inference effects (node_llm_delegation_call_effect)
# raise three terminal-shaped errors for an otherwise-reachable provider:
#   * "finish_reason=length"     — response TRUNCATED at the tier's max_tokens.
#   * "empty message content"    — provider returned a blank message body.
#   * "API returned empty choices array" — provider returned no choices.
# `finish_reason=length` is a CONTEXT/output-budget limit, not a hard refusal:
# a longer-context successor (the cheap_cloud Gemini route declares a 1M-token
# window) can complete what a quantized local model truncated, so it is now
# RETRYABLE and escalates. The original (truncated) and escalated models are both
# recorded in escalation_history (one ModelDelegationEscalationAttempt per tier).
# An empty body / empty choices is left NON-retryable: re-issuing the same prompt
# to a higher tier is unlikely to turn a blank completion into content and would
# burn cloud budget on a probable repeat — the minimal-safe classification.
_NON_RETRYABLE_INFERENCE_ERROR_MARKERS: frozenset[str] = frozenset(
    {
        "empty message content",
        "empty choices array",
    }
)

# Temperature by task type (Task 10, OMN-7040)
_TASK_TEMPERATURE: dict[str, float] = {
    "test": 0.3,
    "document": 0.5,
    "research": 0.7,
}

_logger = logging.getLogger(__name__)

# OMN-13140: resolve the escalation topic from THIS node's contract rather than
# hardcoding it — the publish topic is contract-declared (event_bus.publish_topics
# + published_events), so the runtime DispatchResultApplier routes the typed
# escalation event returned from the escalation branches to it.
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_DELEGATION_ESCALATION_TRIGGERED_SUFFIX = "delegation-escalation-triggered.v1"  # onex-topic-allow: suffix used for contract lookup


def _resolve_escalation_topic() -> str:
    """Return the single escalation publish topic declared by the contract."""
    matches = tuple(
        topic
        for topic in contract_publish_topics(_CONTRACT_PATH)
        if topic.endswith(_DELEGATION_ESCALATION_TRIGGERED_SUFFIX)
    )
    if len(matches) != 1:
        raise RuntimeError(
            f"Contract {_CONTRACT_PATH} must declare exactly one "
            f"event_bus.publish_topics topic ending with "
            f"{_DELEGATION_ESCALATION_TRIGGERED_SUFFIX!r}; found {matches!r}."
        )
    return matches[0]


TOPIC_DELEGATION_ESCALATION_TRIGGERED = _resolve_escalation_topic()


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
    """Persist a single inference attempt's data onto the workflow.

    OMN-13365: ``ModelInferenceResponseData`` carries the three token counts the
    provider reported with no sum constraint between them. Every terminal event
    the orchestrator emits (``ModelDelegationResult`` on the completed/failed/
    all-tiers-exhausted paths and the omnidash compat event) is built from these
    fields, and the canonical ``ModelDelegationResult`` wire DTO enforces
    ``total_tokens == prompt_tokens + completion_tokens``. Reasoning-model
    providers (e.g. ``gemini-2.5-flash``) report a ``total_tokens`` that bundles
    thinking/reasoning tokens NOT split into prompt+completion, so a verbatim
    copy makes ``total != prompt + completion`` and the terminal model
    construction raises ``ValidationError``, crashing the dispatcher with no
    terminal event emitted (silent loss of the outcome).

    Reconcile at this boundary: keep ``prompt_tokens`` and ``completion_tokens``
    exactly as reported (they drive cost estimation independently) and derive
    ``total_tokens`` from their sum so the wire invariant always holds. The
    provider's bundled reasoning-token total is not separately modeled anywhere
    downstream, and every projection consumer already assumes this invariant.
    """
    workflow.inference_intent_in_flight = False
    workflow.inference_content = response.content
    workflow.inference_model_used = response.model_used
    workflow.inference_latency_ms = response.latency_ms
    workflow.inference_prompt_tokens = response.prompt_tokens
    workflow.inference_completion_tokens = response.completion_tokens
    workflow.inference_total_tokens = (
        response.prompt_tokens + response.completion_tokens
    )
    workflow.inference_llm_call_id = response.llm_call_id


def _should_escalate_inference_error(error_message: str) -> bool:
    """Return whether an inference error should retry on a higher tier."""
    normalized = error_message.lower()
    return not any(
        marker in normalized for marker in _NON_RETRYABLE_INFERENCE_ERROR_MARKERS
    )


def _inference_error_failure_class(error_message: str) -> EnumDelegationFailureClass:
    """Classify a retryable inference error into a failure class for the escalation
    event (OMN-13140). The classification is derived from the error text the
    inference effect raised — never a blanket UNKNOWN — so the emitted
    ModelLlmDelegationEscalationTriggeredEvent carries an honest failure_class.
    """
    normalized = error_message.lower()
    if "finish_reason=length" in normalized or "truncat" in normalized:
        return EnumDelegationFailureClass.CONTEXT_TOO_LARGE
    if "timed out" in normalized or "timeout" in normalized:
        return EnumDelegationFailureClass.TIMEOUT
    if "rate limit" in normalized or "429" in normalized:
        return EnumDelegationFailureClass.RATE_LIMITED
    if "401" in normalized or "unauthorized" in normalized or "auth" in normalized:
        return EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    if "unavailable" in normalized or "connection" in normalized or "503" in normalized:
        return EnumDelegationFailureClass.MODEL_UNAVAILABLE
    return EnumDelegationFailureClass.UNKNOWN


def _inference_timeout_seconds(workflow: DelegationWorkflowState) -> float:
    """Return the selected backend timeout in seconds, within wire-model bounds."""
    if workflow.routing_decision is None:
        return 30.0
    return max(1.0, min(600.0, workflow.routing_decision.timeout_ms / 1000.0))


def _build_model_inference_intent(
    *,
    base_url: str,
    model: str,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
    temperature: float,
    timeout_seconds: float,
    correlation_id: UUID,
    api_key_ref: str | None,
    extra_headers: dict[str, str] | None,
    provider_request_options: dict[str, Any],
) -> ModelInferenceIntent:
    # OMN-12815: base_url carries the COMPLETE endpoint URL from the routing
    # authority (decision.endpoint_url); the inference effect posts it verbatim.
    payload: dict[str, Any] = {
        "base_url": base_url,
        "model": model,
        "system_prompt": system_prompt,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "timeout_seconds": timeout_seconds,
        "correlation_id": correlation_id,
        "api_key_ref": api_key_ref,
        "extra_headers": extra_headers,
    }
    if provider_request_options and "provider_request_options" in getattr(
        ModelInferenceIntent, "model_fields", {}
    ):
        payload["provider_request_options"] = provider_request_options
    return ModelInferenceIntent.model_validate(payload)


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
    system_prompt, prompt, provider_request_options = apply_inference_protocol(
        system_prompt=workflow.routing_decision.system_prompt,
        prompt=result.repair_prompt,
        model=workflow.routing_decision.selected_model,
        task_type=workflow.request.task_type,
    )
    return [
        _build_model_inference_intent(
            base_url=workflow.routing_decision.endpoint_url,
            model=workflow.routing_decision.selected_model,
            system_prompt=system_prompt,
            prompt=prompt,
            # OMN-13345: post the contract-declared per-backend output ceiling
            # resolved onto the routing decision (cloud-glm: 65536), NOT the
            # delegation request's max_tokens — that is hard-capped at the 8192
            # DELEGATION_MAX_TOKENS_HARD_LIMIT default and would truncate cloud
            # GLM (finish_reason=length), tanking the quality gate. Same defect
            # class as OMN-13342/#1282 (generation path).
            max_tokens=workflow.routing_decision.max_tokens,
            temperature=temperature,
            timeout_seconds=_inference_timeout_seconds(workflow),
            correlation_id=workflow.correlation_id,
            api_key_ref=workflow.routing_decision.api_key_ref,
            extra_headers=workflow.routing_decision.extra_headers,
            provider_request_options=provider_request_options,
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
        system_prompt, prompt, provider_request_options = apply_inference_protocol(
            system_prompt=decision.system_prompt,
            prompt=workflow.request.prompt,
            model=decision.selected_model,
            task_type=workflow.request.task_type,
        )
        return [
            _build_model_inference_intent(
                base_url=decision.endpoint_url,
                model=decision.selected_model,
                system_prompt=system_prompt,
                prompt=prompt,
                # OMN-13345: post the contract-declared per-backend output
                # ceiling from the routing decision (cloud-glm: 65536), NOT the
                # delegation request's max_tokens. This is the initial dispatch
                # AND the escalation re-entry (ESCALATING -> ROUTED with a fresh
                # decision) — the very path the live prober exercised. The
                # request value is hard-capped at the 8192
                # DELEGATION_MAX_TOKENS_HARD_LIMIT default and truncates cloud
                # GLM (finish_reason=length). Same defect class as
                # OMN-13342/#1282 (generation path).
                max_tokens=decision.max_tokens,
                temperature=temperature,
                timeout_seconds=_inference_timeout_seconds(workflow),
                correlation_id=cid,
                api_key_ref=decision.api_key_ref,
                extra_headers=decision.extra_headers,
                provider_request_options=provider_request_options,
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

            should_escalate_error = _should_escalate_inference_error(
                response.error_message
            )
            if not should_escalate_error:
                terminal_failure_reason = "non_retryable_inference_response"
            elif workflow.escalation_count >= _MAX_INFERENCE_ESCALATION_ATTEMPTS:
                terminal_failure_reason = "max_escalation_attempts_reached"
            elif workflow.current_tier_name is None:
                terminal_failure_reason = "current_tier_unknown"
            else:
                error_task_type = (
                    workflow.request.task_type if workflow.request is not None else None
                )
                next_tier = next_eligible_tier(
                    workflow.current_tier_name,
                    _INFERENCE_ERROR_EXCLUDED_TIERS,
                    task_type=error_task_type,
                )
                if next_tier is None:
                    # OMN-13167: precise reason naming the exhausted policy and
                    # unusable higher tiers. task_type is required for the
                    # diagnostic; without it (legacy task-unaware path) fall back
                    # to the bare token.
                    terminal_failure_reason = (
                        describe_no_higher_tier_available(
                            workflow.current_tier_name,
                            _INFERENCE_ERROR_EXCLUDED_TIERS,
                            task_type=error_task_type,
                        )
                        if error_task_type is not None
                        else NO_HIGHER_TIER_REASON_TOKEN
                    )

            can_escalate = (
                should_escalate_error
                and terminal_failure_reason is None
                and next_tier is not None
            )

            if can_escalate:
                assert next_tier is not None
                # OMN-13140: a retryable inference error with a routable next tier
                # is a real escalation — emit the typed escalation proof BEFORE the
                # state reset clears the failing model. failure_class is derived
                # from the error (timeout/rate-limit/unavailable), never blanket.
                escalation_event = self._build_escalation_event(
                    workflow,
                    failure_class=_inference_error_failure_class(
                        response.error_message
                    ),
                    escalation_reason=response.error_message,
                    model_id=model_used,
                )

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
                    escalation_event,
                ]

            # No escalation possible: terminal FAILED.
            _record_inference_response(workflow, response)
            escalation_metadata = self._escalation_metadata(
                workflow,
                terminal_failure_reason=terminal_failure_reason,
            )
            # OMN-13396: price the failed attempt's measured tokens through the
            # serving tier's typed cost model instead of hardcoding cost_usd=0.0.
            # No premium counterfactual on the failure path (no accepted result to
            # bank a saving against), so cost_savings_usd stays 0.0 — but cost_usd
            # is the real metered cost the failed inference still incurred.
            failed_cost = self._measure_terminal_cost(
                tier_name=workflow.current_tier_name or "",
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                premium_counterfactual=None,
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
                topic=TOPIC_ID_TASK_DELEGATED,
                timestamp=datetime.now(UTC).isoformat(),
                correlation_id=response.correlation_id,
                session_id=None,
                task_type=workflow.request.task_type,
                delegated_to=model_used,
                model_name=workflow.routing_decision.selected_model,
                quality_gate_passed=False,
                quality_gates_failed=[response.error_message],
                cost_usd=failed_cost.cash_cost_usd,
                cost_savings_usd=0.0,
                cost_tier_type=failed_cost.cost_tier_type,
                cost_tier_name=failed_cost.cost_tier_name,
                cost_measurement_source=failed_cost.cost_measurement_source,
                budget_headroom_consumed_usd=failed_cost.headroom_consumed_usd,
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
                    topic=TOPIC_ID_DELEGATION_FAILED,
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
        # OMN-13215: the shelled ``cli_agents`` tier was removed; no tier is
        # excluded from quality-gate escalation now that every tier (including the
        # ceiling) runs over the canonical HTTP inference path.
        excluded_tiers: frozenset[str] = frozenset(),
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
        try:
            required_bar_authority = resolve_required_bar_authority(
                task_type=workflow.request.task_type
            )
        except RequiredBarAuthorityError as exc:
            escalation_metadata = self._escalation_metadata(
                workflow,
                terminal_failure_reason="required_bar_missing",
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
                fallback_to_claude=False,
                failure_reason=f"required_bar_missing: {exc}",
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
                quality_gate_passed=False,
            )
            self._transition(workflow, EnumDelegationState.FAILED)
            return [
                ModelDelegationEvent(
                    topic=TOPIC_ID_DELEGATION_FAILED,
                    payload=delegation_result,
                ),
                compat_event,
            ]

        actual_score = result.quality_score
        score_below_required_bar = actual_score < required_bar_authority.required_bar
        pre_filter_rejected = result.fail_category == "fail_deterministic"
        quality_accepted = not pre_filter_rejected and not score_below_required_bar

        if quality_accepted:
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
                fallback_to_claude=False,
                failure_reason="",
                tokens_to_compliance=tokens_to_compliance,
                compliance_attempts=compliance_attempts,
                **escalation_metadata,
            )

            estimated_claude_cost = estimate_baseline_cost_usd(
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
            )
            # OMN-13396: the candidate (delegated tier) cost is the MEASURED actual
            # cost of the served tokens, not a hardcoded 0.0. free_local -> 0.0;
            # metered -> rate x measured tokens. Same typed-tier-cost computation
            # the projection uses, so baseline-vs-candidate is an honest delta.
            candidate_cost = self._measure_terminal_cost(
                tier_name=workflow.current_tier_name or "",
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
                premium_counterfactual=None,
            )

            compat_event = self._build_compat_event(
                workflow,
                result,
                elapsed_ms,
                tokens_to_compliance,
                compliance_attempts,
                quality_gate_passed=True,
                required_bar_authority=required_bar_authority,
            )

            self._transition(workflow, EnumDelegationState.COMPLETED)
            events.append(
                ModelDelegationEvent(
                    topic=TOPIC_ID_DELEGATION_COMPLETED,
                    payload=delegation_result,
                )
            )
            events.append(
                ModelBaselineIntent(
                    correlation_id=cid,
                    task_type=workflow.request.task_type,
                    baseline_cost_usd=estimated_claude_cost,
                    candidate_cost_usd=candidate_cost.cash_cost_usd,
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
                required_bar=required_bar_authority.required_bar,
                actual_score=actual_score,
                authority_source=required_bar_authority.authority_source,
                score_source=required_bar_authority.score_source,
                failure_reasons=tuple(result.failure_reasons),
                latency_ms=elapsed_ms,
                fallback_recommended=True,
                attempted_at=result.evaluated_at
                if hasattr(result, "evaluated_at") and result.evaluated_at is not None
                else datetime.now(UTC),
                routing_decision_id=workflow.routing_decision.selected_backend_id,
            )
        )

        # Determine why escalation cannot proceed (for audit trail).
        terminal_failure_reason: str | None = None
        next_tier: str | None = None

        if workflow.escalation_count >= max_escalation_attempts:
            terminal_failure_reason = "max_escalation_attempts_reached"
        elif workflow.current_tier_name is None:
            terminal_failure_reason = "current_tier_unknown"
        else:
            next_tier = next_eligible_tier(
                workflow.current_tier_name,
                excluded_tiers,
                task_type=workflow.request.task_type,
            )
            if next_tier is None:
                # OMN-13167: emit a precise reason that names the exhausted
                # task-class escalation policy and the unusable higher tiers,
                # not the bare token. `test`/`research` (and any task class)
                # dead-ended here previously with no indication of which policy
                # or missing tier caused it.
                terminal_failure_reason = describe_no_higher_tier_available(
                    workflow.current_tier_name,
                    excluded_tiers,
                    task_type=workflow.request.task_type,
                )

        can_escalate = terminal_failure_reason is None and next_tier is not None

        if can_escalate:
            assert next_tier is not None
            # OMN-13140: build the terminal escalation proof BEFORE the inference
            # state reset below clears inference_model_used. The quality gate
            # recommended fallback and a routable next tier exists — this is the
            # real escalation decision point, so emit the typed escalation event
            # to the contract-declared escalation topic alongside the re-route.
            escalation_event = self._build_escalation_event(
                workflow,
                failure_class=EnumDelegationFailureClass.QUALITY_GATE_FAILED,
                escalation_reason=self._score_vs_bar_reason(
                    result,
                    required_bar_authority,
                    pre_filter_rejected=pre_filter_rejected,
                ),
            )

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
                escalation_event,
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
            fallback_to_claude=True,
            failure_reason=self._score_vs_bar_reason(
                result,
                required_bar_authority,
                pre_filter_rejected=pre_filter_rejected,
            ),
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
            quality_gate_passed=False,
            required_bar_authority=required_bar_authority,
        )

        self._transition(workflow, EnumDelegationState.FAILED)
        events.append(
            ModelDelegationEvent(
                topic=TOPIC_ID_DELEGATION_FAILED,
                payload=delegation_result,
            )
        )
        events.append(compat_event)
        return events

    def _build_escalation_event(
        self,
        workflow: DelegationWorkflowState,
        *,
        failure_class: EnumDelegationFailureClass,
        escalation_reason: str,
        model_id: str | None = None,
    ) -> ModelLlmDelegationEscalationTriggeredEvent:
        """Build the typed escalation proof for the escalation decision (OMN-13140).

        Emitted from the orchestrator's escalation branches — the only surface
        holding the full escalation decision state (the resolved fallback verdict,
        the routable next tier, ``escalation_count``, ``current_tier_name``, the
        escalating model). ``attempt_number`` is the in-tier attempt number for
        the failing model BEFORE the count is incremented for the next tier, so it
        is always >= 1. ``next_model_id`` is None here: only the next *tier* is
        resolved at this point; the routing reducer resolves the concrete next
        model on the re-route. The concrete escalating model is carried in
        ``model_id``.

        The returned event is published to the contract-declared escalation topic
        by the runtime DispatchResultApplier (publish_topics + published_events),
        never by this handler directly — orchestrators emit, they do not publish.
        """
        assert workflow.request is not None
        resolved_model_id = (
            model_id
            if model_id is not None
            else (workflow.inference_model_used or "unknown")
        )
        correlation_id = str(workflow.correlation_id)
        return ModelLlmDelegationEscalationTriggeredEvent(
            correlation_id=correlation_id,
            causation_id=correlation_id,
            request_id=correlation_id,
            task_type=workflow.request.task_type,
            task_id=None,
            model_id=resolved_model_id,
            attempt_number=workflow.escalation_count + 1,
            failure_class=failure_class,
            escalation_reason=escalation_reason,
            next_model_id=None,
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _score_vs_bar_reason(
        result: ModelQualityGateResult,
        required_bar_authority: RequiredBarAuthority,
        *,
        pre_filter_rejected: bool,
    ) -> str:
        prefix = (
            "pre_filter_rejected" if pre_filter_rejected else "score_below_required_bar"
        )
        detail = (
            f"{prefix}: actual_score={result.quality_score:.3f} "
            f"required_bar={required_bar_authority.required_bar:.3f} "
            f"authority_source={required_bar_authority.authority_source} "
            f"score_source={required_bar_authority.score_source}"
        )
        if result.failure_reasons:
            return f"{detail}; failures={'; '.join(result.failure_reasons)}"
        return detail

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
    def _measure_terminal_cost(
        *,
        tier_name: str,
        prompt_tokens: int,
        completion_tokens: int,
        premium_counterfactual: ModelPremiumCounterfactual | None,
    ) -> ModelActualCostMeasurement:
        """Measure a terminal event's actual cost from the typed tier cost model.

        OMN-13396. The terminal-event construction points (delegate-skill-terminal
        compat events + the baseline intent) previously hardcoded ``cost_usd=0.0``,
        so the live delegation/SEA chain persisted a zero actual cost and the
        savings number was ``counterfactual - 0`` — the full counterfactual,
        overstated by the serving tier's real (non-zero, for metered) cost.

        This mirrors the projection's ``_measure_actual_cost`` exactly by reusing
        the SAME canonical computation (``recompute_actual_cost_and_savings``):
        the serving tier's typed cost model (``ModelTierCost`` /
        ``EnumTierCostType`` from OMN-13234) resolved by tier name from the
        canonical routing registry, priced against the measured token counts —
        ``free_local`` → 0.0, ``metered`` → ``rate_per_1k_usd * tokens / 1000``.
        No parallel formula, no hardcoded 0.0.
        """
        return recompute_actual_cost_and_savings(
            tier_name=tier_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            premium_counterfactual=premium_counterfactual,
        )

    @staticmethod
    def _routing_tiers_hash() -> str | None:
        """SHA-256 of routing_tiers.yaml for replay determinism."""
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
        *,
        quality_gate_passed: bool | None = None,
        required_bar_authority: RequiredBarAuthority | None = None,
    ) -> ModelTaskDelegatedEvent:
        """Build backward-compatible task-delegated.v1 event for omnidash."""
        assert workflow.request is not None
        assert workflow.routing_decision is not None
        assert workflow.inference_model_used is not None

        # OMN-13355: pin the premium counterfactual so cost_savings_usd
        # (= counterfactual_cost_usd - cost_usd) is auditable rather than an opaque
        # estimate. The pinned price + as_of come from the canonical pricing
        # manifest; no live premium call is made.
        premium_counterfactual = build_premium_counterfactual(
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
        )
        # OMN-13396: price the served tokens through the serving tier's typed cost
        # model and bank the HONEST saving (counterfactual - measured actual),
        # mirroring the projection's recompute. cost_usd is the measured actual
        # (free_local -> 0.0; metered -> rate x tokens), not a hardcoded 0.0, so a
        # later projection over this terminal event no longer reads counterfactual
        # minus zero.
        measured_cost = self._measure_terminal_cost(
            tier_name=workflow.current_tier_name or "",
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
            premium_counterfactual=premium_counterfactual,
        )

        history_dicts = tuple(
            attempt.model_dump(mode="json") for attempt in workflow.escalation_history
        )
        accepted = result.passed if quality_gate_passed is None else quality_gate_passed
        quality_gates_checked = (
            format_quality_bar_labels(
                required_bar=required_bar_authority.required_bar,
                actual_score=result.quality_score,
                escalation_count=workflow.escalation_count,
                authority_source=required_bar_authority.authority_source,
                score_source=required_bar_authority.score_source,
                request_override_applied=required_bar_authority.request_override_applied,
                override_within_bounds=required_bar_authority.override_within_bounds,
            )
            if required_bar_authority is not None
            else ["required_bar_missing"]
        )

        return ModelTaskDelegatedEvent(
            topic=TOPIC_ID_TASK_DELEGATED,
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=result.correlation_id,
            session_id=None,
            task_type=workflow.request.task_type,
            delegated_to=workflow.inference_model_used,
            model_name=workflow.routing_decision.selected_model,
            quality_gate_passed=accepted,
            quality_gates_checked=quality_gates_checked,
            quality_gates_failed=[] if accepted else list(result.failure_reasons),
            cost_usd=measured_cost.cash_cost_usd,
            cost_savings_usd=round(measured_cost.cost_savings_usd, 6),
            cost_tier_type=measured_cost.cost_tier_type,
            cost_tier_name=measured_cost.cost_tier_name,
            cost_measurement_source=measured_cost.cost_measurement_source,
            budget_headroom_consumed_usd=measured_cost.headroom_consumed_usd,
            delegation_latency_ms=elapsed_ms,
            llm_call_id=workflow.inference_llm_call_id,
            tokens_to_compliance=tokens_to_compliance,
            compliance_attempts=compliance_attempts,
            pricing_manifest_version=get_manifest_version_int(),
            escalation_count=workflow.escalation_count,
            escalation_history=history_dicts,
            routing_tiers_hash=self._routing_tiers_hash(),
            attempts_count=workflow.escalation_count + 1,
            premium_counterfactual=premium_counterfactual,
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

        # OMN-13396: the remote-agent (A2A) lifecycle carries no token counts and
        # no serving tier — it is not a tier-routed LLM inference. Route it through
        # the same typed-tier-cost measurement so the zero is PROVEN by the cost
        # model (no_cost_model provenance) rather than a silent hardcoded 0.0:
        # measure_terminal_cost with the (unset) tier resolves to no_cost_model and
        # deterministically yields cash_cost_usd == 0.0.
        agent_cost = self._measure_terminal_cost(
            tier_name=workflow.current_tier_name or "",
            prompt_tokens=0,
            completion_tokens=0,
            premium_counterfactual=None,
        )

        compat_event = ModelTaskDelegatedEvent(
            topic=TOPIC_ID_TASK_DELEGATED,
            timestamp=datetime.now(UTC).isoformat(),
            correlation_id=cid,
            session_id=None,
            task_type=workflow.request.task_type,
            delegated_to=delegated_to,
            model_name=delegated_to,
            quality_gate_passed=next_state is EnumDelegationState.COMPLETED,
            quality_gates_checked=["agent-task-lifecycle"],
            quality_gates_failed=[failure_reason] if failure_reason else [],
            cost_usd=agent_cost.cash_cost_usd,
            cost_savings_usd=0.0,
            cost_tier_type=agent_cost.cost_tier_type,
            cost_tier_name=agent_cost.cost_tier_name,
            cost_measurement_source=agent_cost.cost_measurement_source,
            budget_headroom_consumed_usd=agent_cost.headroom_consumed_usd,
            delegation_latency_ms=elapsed_ms,
            llm_call_id=lifecycle_event.remote_task_handle or "",
        )

        topic = (
            TOPIC_ID_DELEGATION_COMPLETED
            if next_state is EnumDelegationState.COMPLETED
            else TOPIC_ID_DELEGATION_FAILED
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
