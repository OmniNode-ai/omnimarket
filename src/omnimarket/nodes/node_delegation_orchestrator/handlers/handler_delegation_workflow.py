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
from typing import Any, ClassVar, cast
from uuid import UUID, uuid4

import yaml
from omnibase_core.models.contracts.subcontracts.model_fsm_state_definition import (
    ModelFSMStateDefinition,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_state_transition import (
    ModelFSMStateTransition,
)
from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
    ModelFSMSubcontract,
)
from omnibase_core.models.delegation.model_agent_task_lifecycle_event import (
    ModelAgentTaskLifecycleEvent,
)
from omnibase_core.models.delegation.model_invocation_command import (
    ModelInvocationCommand,
)
from omnibase_core.models.delegation.wire import ModelPremiumCounterfactual
from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.models.primitives.model_semver import ModelSemVer
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
from omnimarket.nodes.node_delegation_escalation_decision_compute.handlers.handler_escalation_decision import (
    HandlerEscalationDecision,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_FAILED,
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
    recompute_actual_cost_and_savings,
)
from omnimarket.routing.model_escalation_decision_request import (
    ModelEscalationDecisionRequest,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
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


# OMN-13474 (W2 of the OMN-13471 delegation decomposition): the FSM transition
# table is no longer a hardcoded Python literal. It is loaded from this node's
# ``contract.yaml`` ``fsm.transitions`` block — reconciled in W1 (OMN-13473) to be
# the single source of truth — and built into the typed, executor-bound
# ``ModelFSMSubcontract`` (OMN-12835 typed contract-side workflow surface).
#
# OMN-13477 (W5): state advancement is driven directly off that typed FSM via
# ``_advance`` (the sole advance surface), resolving the declared
# ``(from, to)`` transition object from ``_DECLARED_TRANSITIONS`` — the same
# typed transitions the canonical core executor consumes. The imperative
# ``_transition`` guard and its parallel ``_VALID_TRANSITIONS`` set are gone; the
# contract FSM is the runtime advance authority and the per-call-site
# ``_transition(...)`` invocation count is zero.
#
# Note: the ``ROUTED -> ROUTED`` self-loop (OMN-10794) supports the
# schema-compliance loop's repair re-prompts; it is a declared contract edge.

_CONTRACT_FSM_VERSION = ModelSemVer(major=1, minor=0, patch=0)


def _load_fsm_subcontract() -> ModelFSMSubcontract:
    """Build the typed, executor-bound FSM from this node's contract.yaml.

    OMN-13474: parses the contract ``fsm`` block (states / initial_state /
    terminal_states / transitions) into a ``ModelFSMSubcontract`` — the typed
    surface the core FSM executor (``omnibase_core.utils.util_fsm_executor``)
    consumes. The contract is the single source of truth (reconciled in W1,
    OMN-13473); constructing the typed subcontract here makes the declared table
    the execution authority and structurally validates it (initial/terminal
    state membership, transition-state membership, structural uniqueness,
    no-outgoing-from-terminal) at import time. Fails fast on any drift.
    """
    contract_path = Path(__file__).parent.parent / "contract.yaml"
    with contract_path.open(encoding="utf-8") as handle:
        contract_data = yaml.safe_load(handle)

    fsm_block = contract_data["fsm"]
    declared_states: list[str] = list(fsm_block["states"])

    # Validate every declared state is a known EnumDelegationState — fail fast
    # rather than silently dropping an unmapped edge (Operating Rule #8).
    enum_names = {state.value for state in EnumDelegationState}
    unknown_states = set(declared_states) - enum_names
    if unknown_states:
        msg = (
            f"contract.yaml fsm.states declares states with no "
            f"EnumDelegationState member: {sorted(unknown_states)}"
        )
        raise ValueError(msg)

    terminal_states: list[str] = list(fsm_block.get("terminal_states", []))
    state_defs = [
        ModelFSMStateDefinition(
            version=_CONTRACT_FSM_VERSION,
            state_name=state_name,
            state_type="terminal" if state_name in terminal_states else "operational",
            description=state_name,
            is_terminal=state_name in terminal_states,
            # Terminal states are non-recoverable by the FSM subcontract invariant.
            is_recoverable=state_name not in terminal_states,
        )
        for state_name in declared_states
    ]

    transitions = [
        ModelFSMStateTransition(
            version=_CONTRACT_FSM_VERSION,
            transition_name=f"{entry['from']}__to__{entry['to']}__{index}",
            from_state=entry["from"],
            to_state=entry["to"],
            trigger=entry.get("trigger", f"{entry['from']}->{entry['to']}"),
        )
        for index, entry in enumerate(fsm_block["transitions"])
    ]

    return ModelFSMSubcontract(
        version=_CONTRACT_FSM_VERSION,
        state_machine_name="delegation_orchestrator",
        state_machine_version=_CONTRACT_FSM_VERSION,
        description="Delegation orchestrator FSM (contract-driven, OMN-13474)",
        states=state_defs,
        initial_state=fsm_block["initial_state"],
        terminal_states=terminal_states,
        transitions=transitions,
    )


def _build_declared_transitions(
    fsm: ModelFSMSubcontract,
) -> dict[tuple[EnumDelegationState, EnumDelegationState], ModelFSMStateTransition]:
    """Project the typed FSM's declared edges into a ``(from, to)`` lookup.

    OMN-13477 (W5): the runtime no longer hand-drives the FSM through an
    imperative ``_transition`` guard backed by a separate ``from -> {to,...}``
    set. State advancement is driven directly off the typed, executor-bound
    ``ModelFSMSubcontract`` (the W2/OMN-13474 contract binding) — the *same*
    typed transition objects the canonical core executor
    (``omnibase_core.utils.util_fsm_executor.execute_transition``) consumes.

    ``(from_state, to_state)`` is the lookup key: every declared delegation edge
    is unique by that pair (verified against the contract), so a target-keyed
    ``_advance(workflow, target)`` resolves to exactly one declared transition
    — preserving the prior call-site ergonomics (which named the target state)
    while making the contract FSM, not a parallel guard dict, the advance
    authority. Building over the typed transitions also keeps the declared
    ``trigger`` available, so the resolved edge is the contract's own
    executor-bound transition object.
    """
    return {
        (
            EnumDelegationState(transition.from_state),
            EnumDelegationState(transition.to_state),
        ): transition
        for transition in fsm.transitions
    }


# Typed, executor-bound FSM built once at import from the contract (the single
# source of truth, OMN-13474). ``_DECLARED_TRANSITIONS`` is a ``(from, to)``
# projection of its typed transition objects — the runtime advance authority
# (OMN-13477). There is no parallel hand-maintained guard table.
_FSM_SUBCONTRACT: ModelFSMSubcontract = _load_fsm_subcontract()
_DECLARED_TRANSITIONS: dict[
    tuple[EnumDelegationState, EnumDelegationState], ModelFSMStateTransition
] = _build_declared_transitions(_FSM_SUBCONTRACT)


# OMN-13477 (W5): declarative per-step dispatch table. One entry per
# ``handler_routing`` event_model the contract declares (routing_strategy
# ``payload_type_match``), mapping the payload model class to its thin per-step
# FSM handler. ``HandlerDelegationWorkflow.handle`` routes off this table instead
# of a hand-maintained isinstance ladder — each payload type resolves to exactly
# one per-step handler, with no catch-all branch (undeclared types fail closed).
_PER_STEP_DISPATCH: dict[type, str] = {
    ModelDelegationRequest: "handle_delegation_request",
    ModelInvocationCommand: "handle_invocation_command",
    ModelRoutingDecision: "handle_routing_decision",
    ModelInferenceResponseData: "handle_inference_response",
    ModelQualityGateResult: "handle_gate_result",
    ModelAgentTaskLifecycleEvent: "handle_agent_task_lifecycle",
}


def _record_inference_response(
    workflow: DelegationWorkflowState,
    response: ModelInferenceResponseData,
) -> None:
    """Persist a single inference attempt's data onto the workflow.

    OMN-13365: ``ModelInferenceResponseData`` carries the three token counts the
    provider reported with no sum constraint between them. The single canonical
    terminal event the orchestrator emits (``ModelDelegationResult`` on the
    completed / failed / all-tiers-exhausted paths) is built from these fields,
    and the canonical ``ModelDelegationResult`` wire DTO enforces
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


def _normalized_context_pack(request: ModelDelegationRequest) -> str:
    return (getattr(request, "context_pack", "") or "").strip()


def _context_pack_hash_for_event(request: ModelDelegationRequest) -> str:
    if not _normalized_context_pack(request):
        return ""
    return (getattr(request, "context_pack_hash", "") or "").strip()


def _prompt_with_context_pack(request: ModelDelegationRequest, prompt: str) -> str:
    context_pack = _normalized_context_pack(request)
    if not context_pack:
        return prompt
    return f"{context_pack}\n\n{prompt}"


def _evaluate_compliance(
    workflow: DelegationWorkflowState,
    response: ModelInferenceResponseData,
    advance: Callable[
        [DelegationWorkflowState, EnumDelegationState], ModelFSMStateTransition
    ],
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
        advance(workflow, EnumDelegationState.INFERENCE_COMPLETED)
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
    advance(workflow, EnumDelegationState.ROUTED)
    workflow.compliance_attempts += 1
    workflow.inference_intent_in_flight = True
    temperature = _TASK_TEMPERATURE.get(workflow.request.task_type, 0.3)
    system_prompt, prompt, provider_request_options = apply_inference_protocol(
        system_prompt=workflow.routing_decision.system_prompt,
        prompt=_prompt_with_context_pack(workflow.request, result.repair_prompt),
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


@dataclass(frozen=True)
class TerminalEmissionInputs:
    """Single source of truth for a delegation terminal emission (OMN-13475).

    Every terminal site resolves its outcome into ONE of these and hands it to
    ``HandlerDelegationWorkflow._emit_terminal``. That builder is the *only*
    construction site for the canonical ``ModelDelegationResult``: it measures
    cost ONCE and reads the served tokens ONCE. OMN-13629 (WS-F Phase 1)
    collapsed the terminal to a SINGLE canonical event — the legacy compat
    ``ModelTaskDelegatedEvent`` co-writer was deleted, so the OMN-13408
    token/cost-zeroing divergence (two co-writers of one row) is structurally
    impossible: there is now exactly one writer.

    ``premium_counterfactual`` is supplied only when a saving should be banked
    (the accepted/completed path); on failure/agent paths it is ``None`` so the
    derived saving is 0.0 by construction.
    """

    completed: bool
    correlation_id: UUID
    task_type: str
    model_used: str
    endpoint_url: str
    content: str
    quality_passed: bool
    quality_score: float
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    fallback_to_claude: bool
    failure_reason: str
    tokens_to_compliance: int
    compliance_attempts: int
    # Cost-measurement inputs (priced once inside _emit_terminal).
    cost_tier_name: str
    premium_counterfactual: ModelPremiumCounterfactual | None
    # Escalation / audit metadata (carried on the canonical terminal).
    escalation_count: int
    escalation_history: tuple[dict[str, object], ...]
    terminal_failure_reason: str | None
    routing_tiers_hash: str | None
    escalation_config_hash: str | None
    attempts_count: int
    # Compat-event-only descriptive fields.
    model_name: str
    session_id: UUID | None
    quality_gates_checked: list[str]
    quality_gates_failed: list[str]
    llm_call_id: str
    context_pack_hash: str
    # OMN-13535: metered spend already banked on PRIOR attempted tiers (rejected /
    # failed inference attempts that escalated). The terminal adds this to the
    # final tier's measured cost so cost_usd reflects the TOTAL metered spend
    # across every attempted tier — a metered tier that was attempted-but-rejected
    # (escalated to a free tier) still contributes its real cost to the row.
    # Defaulted so the A2A / non-escalating call sites are unaffected.
    prior_attempt_cost_usd: float = 0.0
    prior_attempt_prompt_tokens: int = 0
    prior_attempt_completion_tokens: int = 0


@dataclass(frozen=True)
class _HoistedTierCost:
    """The metered ``escalation_history`` tier hoisted onto a residual
    null-top-level FAILED terminal (OMN-13408 emitter hoist).

    ``measurement`` is re-derived from the tier name + served tokens through the
    SAME ``recompute_actual_cost_and_savings`` the completed/projection paths use
    — the per-attempt ``cost_usd`` stamped in history is the audit copy, but the
    authoritative top-level cost is re-priced here so a single canonical formula
    owns it (no trust of a stale stamped value).
    """

    measurement: ModelActualCostMeasurement
    prompt_tokens: int
    completion_tokens: int


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
    # OMN-13644: context-pack hash captured ONCE at request acceptance so it
    # persists onto EVERY terminal (COMPLETED and FAILED/ESCALATED) — escalation
    # re-routing or prompt-text loss between attempts must NOT drop it. Reading it
    # off ``request`` at terminal-build time would re-derive a value that can be
    # lost if the request is not intact; storing it here pins the OFF/ON-arm value
    # from acceptance. Defaults to '' (OFF-arm: no context pack supplied).
    context_pack_hash: str = ""
    gate_result: ModelQualityGateResult | None = None
    started_at_ns: int = field(default_factory=time.monotonic_ns)
    # Compliance-loop counters (OMN-10794). The orchestrator owns the loop,
    # ``compliance_attempts`` counts the inference attempts it has issued so
    # far (1 = first attempt) and ``accumulated_tokens`` is the running sum
    # of tokens across all attempts. Both are forwarded onto the terminal
    # canonical ModelDelegationResult.
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
    # OMN-13535: cumulative metered spend across ALL attempted tiers (not just the
    # final accepted one). Each attempt recorded into ``escalation_history`` adds
    # its served tokens + measured metered cost here BEFORE the inference state is
    # reset for the next tier. The terminal reports this cumulative spend so a
    # metered tier that was attempted-but-rejected (escalated to a free tier)
    # still contributes its real ``cost_usd`` to the projection — the prior
    # behavior dropped it, leaving the row at ``cost_usd=0`` despite a real
    # metered cloud call.
    cumulative_attempt_cost_usd: float = 0.0
    cumulative_attempt_prompt_tokens: int = 0
    cumulative_attempt_completion_tokens: int = 0


class HandlerDelegationWorkflow:
    """Delegation orchestrator with correlation_id-keyed FSM state machine.

    Each delegation request creates a workflow keyed by its correlation_id.
    Events are matched to workflows by correlation_id and processed through
    the FSM. Duplicate or out-of-order events are handled safely.
    """

    _shared_workflows: ClassVar[dict[UUID, DelegationWorkflowState]] = {}

    # OMN-13476: the escalation/tier decision is owned by a stateless COMPUTE
    # node. The orchestrator resolves the config-dependent inputs (next eligible
    # tier + no-higher-tier reason, which read the routing contract+overlay) and
    # delegates the deterministic verdict to this handler.
    _escalation_decider: ClassVar[HandlerEscalationDecision] = (
        HandlerEscalationDecision()
    )

    def __init__(
        self,
        workflows: MutableMapping[UUID, DelegationWorkflowState] | None = None,
    ) -> None:
        self._workflows = workflows if workflows is not None else self._shared_workflows

    @property
    def workflows(self) -> MutableMapping[UUID, DelegationWorkflowState]:
        """Expose workflows for testing/observability."""
        return self._workflows

    def _advance(
        self,
        workflow: DelegationWorkflowState,
        target: EnumDelegationState,
    ) -> ModelFSMStateTransition:
        """Advance ``workflow`` to ``target`` through the typed contract FSM.

        OMN-13477 (W5): the sole state-advance surface. It resolves the declared
        ``(from_state, target)`` edge from the typed, executor-bound
        ``_FSM_SUBCONTRACT`` (the OMN-13474 contract binding) — the same typed
        transition object the canonical core FSM executor consumes — and rejects
        any edge the contract does not declare. This replaces the imperative
        ``_transition`` guard (and its parallel ``_VALID_TRANSITIONS`` set): the
        contract FSM, not hand-maintained handler logic, is now the transition
        authority. Returns the resolved typed transition so callers can assert
        on the contract edge they drove.
        """
        transition = _DECLARED_TRANSITIONS.get((workflow.state, target))
        if transition is None:
            msg = (
                f"Invalid state transition: {workflow.state} -> {target} "
                f"for correlation_id={workflow.correlation_id}"
            )
            raise InvalidStateTransitionError(msg)
        workflow.state = target
        return transition

    def _decide_escalation(
        self,
        workflow: DelegationWorkflowState,
        *,
        max_escalation_attempts: int,
        excluded_tiers: frozenset[str],
        error_retryable: bool,
        non_retryable_reason: str,
        task_type: str | None,
    ) -> ModelEscalationDecisionResult:
        """Delegate the escalate-or-terminate verdict to the COMPUTE (OMN-13476).

        The config-dependent inputs are resolved here — the orchestrator owns the
        routing-contract I/O. ``next_eligible_tier`` /
        ``describe_no_higher_tier_available`` read ``routing_tiers.yaml`` and the
        task-class contract; their plain results are handed to the stateless
        COMPUTE, which applies the deterministic decision precedence. The
        relocation preserves behavior exactly: the COMPUTE checks retryability,
        budget, current-tier identifiability, and ladder exhaustion in the same
        order the inline branches did.

        When ``task_type`` is None (the legacy task-unaware inference-error path)
        the precise no-higher-tier diagnostic cannot be built, so the bare
        ``NO_HIGHER_TIER_REASON_TOKEN`` is used — matching the prior inline
        behavior. The quality-gate path always supplies a ``task_type``.
        """
        next_tier: str | None = None
        no_higher_tier_reason: str | None = None

        # Only resolve a next tier when the cheap pure preconditions allow it; the
        # COMPUTE re-checks them, but resolving the tier here would be wasted I/O
        # (and, for current_tier_name=None, impossible).
        if (
            error_retryable
            and workflow.escalation_count < max_escalation_attempts
            and workflow.current_tier_name is not None
        ):
            next_tier = next_eligible_tier(
                workflow.current_tier_name,
                excluded_tiers,
                task_type=task_type,
            )
            if next_tier is None:
                no_higher_tier_reason = (
                    describe_no_higher_tier_available(
                        workflow.current_tier_name,
                        excluded_tiers,
                        task_type=task_type,
                    )
                    if task_type is not None
                    else NO_HIGHER_TIER_REASON_TOKEN
                )

        return self._escalation_decider.handle(
            ModelEscalationDecisionRequest(
                escalation_count=workflow.escalation_count,
                max_escalation_attempts=max_escalation_attempts,
                current_tier_name=workflow.current_tier_name,
                error_retryable=error_retryable,
                next_tier_name=next_tier,
                non_retryable_reason=non_retryable_reason,
                no_higher_tier_reason=no_higher_tier_reason,
            )
        )

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
            # OMN-13644: pin the context-pack hash from acceptance so every
            # terminal carries it regardless of later escalation / request loss.
            context_pack_hash=_context_pack_hash_for_event(request),
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

        self._advance(workflow, EnumDelegationState.ROUTED)
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
            self._advance(workflow, EnumDelegationState.ROUTED)
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
            prompt=_prompt_with_context_pack(workflow.request, workflow.request.prompt),
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
            # deciding whether to escalate or terminate. OMN-13535: the inference
            # error still carries the served usage (OMN-13408 InferenceUsageError
            # threads truncation/empty-content tokens through), so bank this
            # attempt's metered spend into the cumulative accumulators before the
            # state reset below overwrites workflow.inference_* for the next tier.
            attempt_cost_usd = self._record_escalation_attempt(
                workflow,
                ModelDelegationEscalationAttempt(
                    tier_name=workflow.current_tier_name or "unknown",
                    model_used=model_used,
                    quality_score=0.0,
                    failure_reasons=(response.error_message,),
                    latency_ms=elapsed_ms,
                    fallback_recommended=True,
                    attempted_at=datetime.now(UTC),
                    routing_decision_id=workflow.routing_decision.selected_backend_id,
                ),
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
            )

            # OMN-13476: the escalate-or-terminate decision is owned by the
            # node_delegation_escalation_decision_compute COMPUTE. Infra errors
            # (auth failure, connection refused, timeout) are retryable at the
            # next tier; empty body / empty choices are not. The orchestrator
            # resolves the routing-contract inputs inside ``_decide_escalation``
            # and delegates the verdict — behavior identical to the prior inline
            # branch (retryability → budget → current-tier → ladder precedence).
            error_task_type = (
                workflow.request.task_type if workflow.request is not None else None
            )
            decision = self._decide_escalation(
                workflow,
                max_escalation_attempts=_MAX_INFERENCE_ESCALATION_ATTEMPTS,
                excluded_tiers=_INFERENCE_ERROR_EXCLUDED_TIERS,
                error_retryable=_should_escalate_inference_error(
                    response.error_message
                ),
                non_retryable_reason="non_retryable_inference_response",
                task_type=error_task_type,
            )
            terminal_failure_reason = decision.terminal_failure_reason
            next_tier = decision.next_tier_name

            if decision.can_escalate:
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

                # OMN-13535: this attempt ran and a NEXT tier will run, so bank its
                # metered spend into the cumulative totals BEFORE the state reset
                # below discards workflow.inference_*. The terminal then reports
                # final-tier-cost + cumulative.
                self._bank_attempt_spend(
                    workflow,
                    cost_usd=attempt_cost_usd,
                    prompt_tokens=response.prompt_tokens,
                    completion_tokens=response.completion_tokens,
                )

                self._advance(workflow, EnumDelegationState.ESCALATING)
                workflow.escalation_count += 1

                workflow.inference_content = None
                workflow.inference_model_used = None
                workflow.inference_intent_in_flight = False
                workflow.routing_decision = None

                self._advance(workflow, EnumDelegationState.ROUTED)
                assert workflow.request is not None
                return [
                    ModelRoutingIntent(
                        payload=workflow.request,
                        min_tier_name=next_tier,
                    ),
                    escalation_event,
                ]

            # No escalation possible: terminal FAILED.
            # OMN-13408/OMN-13365: _record_inference_response reconciles the
            # served tokens onto workflow.inference_* (deriving total from
            # prompt + completion so a reasoning model's bundled total cannot
            # crash the wire DTO with no terminal emitted). The single terminal
            # builder then reads those reconciled token counts once.
            _record_inference_response(workflow, response)
            terminal_inputs = TerminalEmissionInputs(
                completed=False,
                correlation_id=response.correlation_id,
                task_type=workflow.request.task_type,
                model_used=model_used,
                endpoint_url=workflow.routing_decision.endpoint_url,
                content=response.content,
                quality_passed=False,
                quality_score=0.0,
                latency_ms=elapsed_ms,
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
                total_tokens=workflow.inference_total_tokens,
                fallback_to_claude=False,
                failure_reason=response.error_message,
                tokens_to_compliance=workflow.accumulated_tokens,
                compliance_attempts=workflow.compliance_attempts or 1,
                # No premium counterfactual on the failure path — no accepted
                # result to bank a saving against, so cost_savings_usd is 0.0
                # while cost_usd is the real metered cost the failed inference
                # still incurred (priced once inside _emit_terminal).
                cost_tier_name=workflow.current_tier_name or "",
                premium_counterfactual=None,
                escalation_count=workflow.escalation_count,
                escalation_history=tuple(
                    attempt.model_dump(mode="json")
                    for attempt in workflow.escalation_history
                ),
                terminal_failure_reason=terminal_failure_reason,
                routing_tiers_hash=self._routing_tiers_hash(),
                escalation_config_hash=None,
                attempts_count=workflow.escalation_count + 1,
                model_name=workflow.routing_decision.selected_model,
                session_id=None,
                # Pre-gate inference failure: no quality gate ran, so report the
                # compat DTO's documented default gate set as "checked".
                quality_gates_checked=["length", "refusal", "markers"],
                quality_gates_failed=[response.error_message],
                llm_call_id=response.llm_call_id,
                context_pack_hash=workflow.context_pack_hash,
                # OMN-13535: metered spend banked on every PRIOR attempted tier.
                # The CURRENT failing attempt is re-priced by _emit_terminal from
                # cost_tier_name + the current tokens above, so it is not banked
                # here (no double count) — cumulative holds only earlier tiers.
                prior_attempt_cost_usd=workflow.cumulative_attempt_cost_usd,
                prior_attempt_prompt_tokens=workflow.cumulative_attempt_prompt_tokens,
                prior_attempt_completion_tokens=workflow.cumulative_attempt_completion_tokens,
            )
            self._advance(workflow, EnumDelegationState.FAILED)
            return self._emit_terminal(terminal_inputs)

        # Legacy path: no compliance loop, single attempt.
        if workflow.request.output_schema_key is None:
            self._advance(workflow, EnumDelegationState.INFERENCE_COMPLETED)
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
        return _evaluate_compliance(workflow, response, self._advance)

    @staticmethod
    def _terminal_failed_fields(
        workflow: DelegationWorkflowState,
    ) -> tuple[str, str, str]:
        """Resolve fail-closed (model_used, endpoint_url, content) for a terminal
        FAILED event.

        OMN-13470 / OMN-13140: the terminal ``ModelDelegationResult`` wire DTO
        requires non-null ``model_used``, ``endpoint_url``, and ``content``. On an
        all-tiers-failed / judge-failed terminal these workflow fields can be
        ``None`` (e.g. the inference attempt never produced content, or the
        routing decision was reset on a prior escalation), which made the terminal
        construction raise ``ValidationError`` — crashing the dispatcher with NO
        terminal event emitted (silent loss; the all-tiers-failed HWM stayed 0).
        Fail closed to explicit sentinel strings so a valid terminal event is
        ALWAYS emitted; the failure reason carries the real cause.
        """
        model_used = workflow.inference_model_used or "none"
        endpoint_url = (
            workflow.routing_decision.endpoint_url
            if workflow.routing_decision is not None
            else "none"
        )
        content = workflow.inference_content if workflow.inference_content else ""
        return model_used, endpoint_url, content

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
        1. The single canonical delegation terminal event (completed or failed)
        2. A baseline comparison intent for savings computation (pass only)

        OMN-13629: the legacy task-delegated.v1 compat event is no longer emitted.
        """
        cid = result.correlation_id
        workflow = self._workflows.get(cid)
        if workflow is None:
            return []

        if workflow.state != EnumDelegationState.INFERENCE_COMPLETED:
            return []

        self._advance(workflow, EnumDelegationState.GATE_EVALUATED)
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
            terminal_inputs = self._gate_terminal_inputs(
                workflow,
                result,
                elapsed_ms,
                tokens_to_compliance,
                compliance_attempts,
                completed=False,
                fallback_to_claude=False,
                failure_reason=f"required_bar_missing: {exc}",
                terminal_failure_reason="required_bar_missing",
                required_bar_authority=None,
            )
            self._advance(workflow, EnumDelegationState.FAILED)
            return self._emit_terminal(terminal_inputs)

        actual_score = result.quality_score
        score_below_required_bar = actual_score < required_bar_authority.required_bar
        pre_filter_rejected = result.fail_category == "fail_deterministic"
        # OMN-13409: quality_accepted requires result.passed in addition to the
        # score-threshold and deterministic-rejection checks. Before this fix the
        # orchestrator recomputed acceptance from fail_category + score alone and
        # ignored result.passed, so a heuristic refusal (e.g. "No.", "NO") with a
        # score at or above the required_bar was accepted and delegation-completed
        # was emitted with quality_passed=True. result.passed is the quality gate's
        # authoritative verdict — it is False whenever any heuristic with adequacy
        # authority fails (including the extended no_refusal pre-pass) — and the
        # orchestrator must honour it.
        quality_accepted = (
            not pre_filter_rejected and not score_below_required_bar and result.passed
        )

        if quality_accepted:
            # --- PASSED: complete as before ---
            terminal_inputs = self._gate_terminal_inputs(
                workflow,
                result,
                elapsed_ms,
                tokens_to_compliance,
                compliance_attempts,
                completed=True,
                fallback_to_claude=False,
                failure_reason="",
                terminal_failure_reason=None,
                required_bar_authority=required_bar_authority,
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

            self._advance(workflow, EnumDelegationState.COMPLETED)
            # OMN-13629 (WS-F Phase 1): the terminal is now a single canonical
            # event from the one builder. Emission order is
            # [completed-terminal, baseline-intent]; the legacy compat twin that
            # previously trailed the baseline intent is gone.
            events.extend(self._emit_terminal(terminal_inputs))
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
            return events

        # --- FAILED: evaluate escalation (OMN-12254) ---

        # Record this tier attempt in escalation history. OMN-13535: this is the
        # ATTEMPTED-but-rejected tier (e.g. metered GLM whose output the quality
        # gate rejected). Its inference call really ran and incurred tokens/cost;
        # price + stamp it into escalation_history now. If escalation proceeds
        # (below), the spend is banked into the cumulative totals; if this is the
        # terminal attempt, ``_emit_terminal`` re-prices it from the same
        # current-tier tokens, so it is NOT double-counted.
        rejected_attempt_cost_usd = self._record_escalation_attempt(
            workflow,
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
            ),
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
        )

        # OMN-13476: a sub-bar quality result is always retryable on a higher
        # tier, so the decision reduces to budget / current-tier / ladder. The
        # orchestrator resolves the routing-contract inputs inside
        # ``_decide_escalation`` and delegates the verdict to the COMPUTE —
        # behavior identical to the prior inline branch (OMN-13167 precise
        # no-higher-tier reason still emitted, sourced from the same
        # describe_no_higher_tier_available call).
        decision = self._decide_escalation(
            workflow,
            max_escalation_attempts=max_escalation_attempts,
            excluded_tiers=excluded_tiers,
            error_retryable=True,
            non_retryable_reason="non_retryable_quality_result",
            task_type=workflow.request.task_type,
        )
        terminal_failure_reason = decision.terminal_failure_reason
        next_tier = decision.next_tier_name

        if decision.can_escalate:
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

            # OMN-13535: the rejected tier ran and a NEXT tier will run, so bank
            # its metered spend into the cumulative totals BEFORE the reset below
            # discards workflow.inference_*. The terminal then reports
            # final-tier-cost + cumulative.
            self._bank_attempt_spend(
                workflow,
                cost_usd=rejected_attempt_cost_usd,
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
            )

            self._advance(workflow, EnumDelegationState.ESCALATING)
            workflow.escalation_count += 1

            # Reset inference state for the new attempt.
            workflow.inference_content = None
            workflow.inference_model_used = None
            workflow.inference_intent_in_flight = False
            workflow.routing_decision = None

            # Transition to ROUTED and emit new routing intent with tier override.
            self._advance(workflow, EnumDelegationState.ROUTED)
            assert workflow.request is not None
            return [
                ModelRoutingIntent(
                    payload=workflow.request,
                    min_tier_name=next_tier,
                ),
                escalation_event,
            ]

        # Cannot escalate: terminal FAILED with reason.
        terminal_inputs = self._gate_terminal_inputs(
            workflow,
            result,
            elapsed_ms,
            tokens_to_compliance,
            compliance_attempts,
            completed=False,
            fallback_to_claude=True,
            failure_reason=self._score_vs_bar_reason(
                result,
                required_bar_authority,
                pre_filter_rejected=pre_filter_rejected,
            ),
            terminal_failure_reason=terminal_failure_reason,
            required_bar_authority=required_bar_authority,
        )

        self._advance(workflow, EnumDelegationState.FAILED)
        events.extend(self._emit_terminal(terminal_inputs))
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

    def _record_escalation_attempt(
        self,
        workflow: DelegationWorkflowState,
        attempt: ModelDelegationEscalationAttempt,
        *,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> float:
        """Price one attempt and append it to ``escalation_history``.

        OMN-13535: the served tokens for an ATTEMPTED tier are about to be lost —
        the inference state is reset for the next tier's call right after this, so
        ``workflow.inference_*`` is overwritten. Price this attempt's served
        tokens through its serving tier's typed cost model ONCE here (the same
        ``recompute_actual_cost_and_savings`` the projection uses) and stamp the
        priced cost + served tokens onto the typed attempt record so the per-tier
        spend is durable in ``escalation_history``.

        This appends history only. Banking into the workflow's cumulative
        accumulators is done explicitly by the ESCALATION branches (via
        ``_bank_attempt_spend``) — never here — so a TERMINAL-fail attempt (which
        is re-priced by ``_emit_terminal`` from the same current-tier tokens) is
        not double-counted. Returns the measured metered cost for the caller to
        bank when it knows the attempt is non-terminal.
        """
        measurement = self._measure_terminal_cost(
            tier_name=attempt.tier_name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            premium_counterfactual=None,
        )
        priced_attempt = attempt.model_copy(
            update={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cost_usd": measurement.cash_cost_usd,
            }
        )
        workflow.escalation_history.append(priced_attempt)
        return measurement.cash_cost_usd

    @staticmethod
    def _bank_attempt_spend(
        workflow: DelegationWorkflowState,
        *,
        cost_usd: float,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Add a non-terminal attempt's metered spend to the cumulative totals.

        OMN-13535: called only on the ESCALATION branches (the attempt ran, was
        rejected/failed, and a NEXT tier will run). The terminal reports
        ``final_tier_cost + cumulative`` so a metered tier that was attempted then
        escalated past still contributes its real cost to the projection row.
        """
        workflow.cumulative_attempt_cost_usd += cost_usd
        workflow.cumulative_attempt_prompt_tokens += prompt_tokens
        workflow.cumulative_attempt_completion_tokens += completion_tokens

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

    @staticmethod
    def _hoist_metered_history_tier(
        escalation_history: tuple[dict[str, object], ...],
    ) -> _HoistedTierCost | None:
        """Find the winning (last) metered ``escalation_history`` tier and re-price it.

        OMN-13408 emitter hoist. Scans ``escalation_history`` from the END
        (the winning/last attempt the FAILED terminal exhausted on) for the first
        entry whose tier name + served tokens re-price to a metered cost > 0
        through the canonical ``recompute_actual_cost_and_savings``. Returns the
        re-priced measurement plus the served tokens, or ``None`` when no metered
        tier with served tokens exists (e.g. a free_local-only ladder — the
        terminal honestly stays 0).

        The per-attempt ``cost_usd`` stamped in history is NOT trusted as the
        top-level value; the cost is re-derived from tier + tokens so one
        canonical formula owns the authoritative top-level cost.
        """
        for entry in reversed(escalation_history):
            tier_name = entry.get("tier_name")
            if not isinstance(tier_name, str) or not tier_name:
                continue
            prompt_tokens = entry.get("prompt_tokens")
            completion_tokens = entry.get("completion_tokens")
            if not isinstance(prompt_tokens, int) or not isinstance(
                completion_tokens, int
            ):
                continue
            if prompt_tokens <= 0 and completion_tokens <= 0:
                continue
            measurement = recompute_actual_cost_and_savings(
                tier_name=tier_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                premium_counterfactual=None,
            )
            if measurement.cash_cost_usd > 0.0:
                return _HoistedTierCost(
                    measurement=measurement,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                )
        return None

    def _emit_terminal(self, inputs: TerminalEmissionInputs) -> list[BaseModel]:
        """ONE builder for the single canonical terminal event (OMN-13629).

        This is the sole construction site for the canonical
        ``ModelDelegationResult`` (wrapped in a ``ModelDelegationEvent`` on the
        completed/failed topic). Cost is measured exactly once here from the
        inputs' token counts + serving tier.

        OMN-13629 (WS-F Phase 1): the legacy compat ``ModelTaskDelegatedEvent``
        co-writer (``task-delegated.v1``) was DELETED. A single terminal outcome
        now emits a single canonical event — collapsing the two-co-writers-of-one
        -row shim that drove the OMN-13408 / 13335 / 13475 / 13535 divergence bug
        class. The savings + delegation projections consume the canonical
        ``delegation-{completed,failed}.v1`` directly (no compat hop), so there is
        no second wire path that could carry a divergent cost or token count.

        The OMN-13335 ``max(0.0, …)`` clamp below is retained as an HONEST VALUE
        FLOOR, not a crash-avoidance shim: ``ModelDelegationResult`` does not
        surface a ``ge=0`` savings field, so a negative subtraction no longer
        crashes terminal construction. The clamp now only keeps a derived saving
        from going negative on an escalation that burned metered budget — that
        spend is already captured in ``cumulative_attempt_cost``; a negative
        "saving" would be dishonest, never terminal-suppressing.
        """
        cost = self._measure_terminal_cost(
            tier_name=inputs.cost_tier_name,
            prompt_tokens=inputs.prompt_tokens,
            completion_tokens=inputs.completion_tokens,
            premium_counterfactual=inputs.premium_counterfactual,
        )

        # OMN-13535: total metered spend = the FINAL tier's measured cost PLUS the
        # metered cost already banked on every PRIOR attempted tier (rejected /
        # failed inference attempts that escalated). Without this, a metered tier
        # that ran and was rejected — escalating to a cheaper/free tier — would
        # contribute $0 to the row because the terminal reflects only the final
        # accepted tier (free → 0). The per-attempt costs are also carried in
        # ``escalation_history`` so the projection can re-derive the same total.
        total_cost_usd = cost.cash_cost_usd + inputs.prior_attempt_cost_usd
        cumulative_input_tokens = (
            inputs.prompt_tokens + inputs.prior_attempt_prompt_tokens
        )
        cumulative_output_tokens = (
            inputs.completion_tokens + inputs.prior_attempt_completion_tokens
        )
        # Honest savings subtract the TOTAL spend across all tiers, not just the
        # final tier's, so an escalation that burned metered budget before landing
        # on a free tier does not overstate the saving. OMN-13335 / OMN-13629: when
        # a metered prior tier's spend exceeds the final tier's counterfactual
        # saving (e.g. a FAILED / escalation-exhausted terminal whose final tier
        # carries no premium counterfactual, so ``cost.cost_savings_usd == 0.0``),
        # this subtraction goes NEGATIVE. Savings cannot be negative — an
        # escalation that burned metered budget did not "save" negative money; that
        # spend is already captured in ``cumulative_attempt_cost``. Clamp to the
        # honest floor of 0.0 so the derived saving is an honest non-negative
        # value. (Pre-OMN-13629 the legacy ``ModelTaskDelegatedEvent.cost_savings_usd``
        # pinned ``ge=0.0`` and a negative value crashed terminal construction,
        # suppressing the whole terminal — the silent loss live-proven by CID
        # 67d2bfc8. The compat event is gone, so the clamp is now a value floor,
        # never terminal-suppressing.)
        total_savings_usd = max(
            0.0, cost.cost_savings_usd - inputs.prior_attempt_cost_usd
        )

        # The served tokens the canonical terminal reports. Defaults to the
        # top-level inputs; overridden by the hoist below on the residual
        # null-top-level FAILED shape.
        served_input_tokens = inputs.prompt_tokens
        served_output_tokens = inputs.completion_tokens
        served_total_tokens = inputs.total_tokens

        # OMN-13408 (emitter hoist): on a FAILED terminal whose TOP-LEVEL cost,
        # tokens, and serving tier all resolved to null/zero — the residual shape
        # live-proven by clean-room CID 1f969398 (dev lane 2026-06-24), where the
        # real metered spend survived ONLY in ``escalation_history`` because the
        # workflow's ``current_tier_name`` / ``inference_*`` were not intact at
        # terminal-build time — hoist the winning (last) metered escalation_history
        # tier's measured cost + serving tier + served tokens into the top-level.
        #
        # This REPLACES the zero with the already-priced authoritative value once;
        # it never re-adds, so the OMN-13535 no-double-count invariant holds. It
        # only fires when the measured top-level total is 0 AND no served tokens
        # were carried up — i.e. exactly the bug condition. When the intact path
        # already populated the top level (cost > 0 or tokens > 0), the hoist is a
        # no-op and the authoritative summed total is preserved verbatim. A
        # free_local-only history has no metered winner to hoist, so the terminal
        # honestly stays 0 by the cost model.
        if (
            not inputs.completed
            and total_cost_usd <= 0.0
            and cumulative_input_tokens == 0
            and cumulative_output_tokens == 0
        ):
            hoisted = self._hoist_metered_history_tier(inputs.escalation_history)
            if hoisted is not None:
                cost = hoisted.measurement
                total_cost_usd = cost.cash_cost_usd
                served_input_tokens = hoisted.prompt_tokens
                served_output_tokens = hoisted.completion_tokens
                # Reconcile the canonical wire invariant (total == prompt +
                # completion) against the hoisted served tokens — the residual
                # shape carried total_tokens=0, which would violate the DTO.
                served_total_tokens = served_input_tokens + served_output_tokens
                cumulative_input_tokens = served_input_tokens
                cumulative_output_tokens = served_output_tokens
                # No counterfactual on the failure path -> savings stays 0 (never
                # counterfactual-minus-0); the hoisted measurement already has
                # cost_savings_usd == 0.0 (premium_counterfactual=None below).
                total_savings_usd = cost.cost_savings_usd

        delegation_result = ModelDelegationResult(
            correlation_id=inputs.correlation_id,
            task_type=inputs.task_type,
            model_used=inputs.model_used,
            endpoint_url=inputs.endpoint_url,
            content=inputs.content,
            quality_passed=inputs.quality_passed,
            quality_score=inputs.quality_score,
            latency_ms=inputs.latency_ms,
            prompt_tokens=served_input_tokens,
            completion_tokens=served_output_tokens,
            total_tokens=served_total_tokens,
            fallback_to_claude=inputs.fallback_to_claude,
            failure_reason=inputs.failure_reason,
            tokens_to_compliance=inputs.tokens_to_compliance,
            compliance_attempts=inputs.compliance_attempts,
            escalation_count=inputs.escalation_count,
            escalation_history=inputs.escalation_history,
            terminal_failure_reason=inputs.terminal_failure_reason,
            routing_tiers_hash=inputs.routing_tiers_hash,
            escalation_config_hash=inputs.escalation_config_hash,
            attempts_count=inputs.attempts_count,
            # Cumulative spend across ALL attempted tiers (OMN-13535) — the final
            # tier's measured cost plus the prior attempts' banked metered cost.
            cumulative_attempt_cost=total_cost_usd,
            cumulative_input_tokens=cumulative_input_tokens,
            cumulative_output_tokens=cumulative_output_tokens,
            final_attempt_cost=cost.cash_cost_usd,
            # OMN-13644: persist the context-pack hash (captured at acceptance,
            # threaded through TerminalEmissionInputs) onto the canonical terminal
            # so COMPLETED and FAILED/ESCALATED rows both carry it. '' is the
            # honest OFF-arm default (no context pack supplied).
            context_pack_hash=inputs.context_pack_hash,
        )

        # OMN-13629 (WS-F Phase 1): the legacy compat ``ModelTaskDelegatedEvent``
        # (``task-delegated.v1``) is no longer constructed. ``total_savings_usd``
        # remains the honest non-negative derived saving for documentation /
        # invariants but is no longer carried on a wire event — the canonical
        # ``ModelDelegationResult`` carries the cumulative spend
        # (``cumulative_attempt_cost``) + the pinned counterfactual is rebuilt by
        # the savings projection from the served tokens, so the saving is
        # re-derived downstream from the same authoritative cost/token figures.
        assert total_savings_usd >= 0.0  # honest floor invariant (OMN-13335)

        topic = (
            TOPIC_ID_DELEGATION_COMPLETED
            if inputs.completed
            else TOPIC_ID_DELEGATION_FAILED
        )
        return [
            ModelDelegationEvent(topic=topic, payload=delegation_result),
        ]

    def _gate_terminal_inputs(
        self,
        workflow: DelegationWorkflowState,
        result: ModelQualityGateResult,
        elapsed_ms: int,
        tokens_to_compliance: int,
        compliance_attempts: int,
        *,
        completed: bool,
        fallback_to_claude: bool,
        failure_reason: str,
        terminal_failure_reason: str | None,
        required_bar_authority: RequiredBarAuthority | None,
    ) -> TerminalEmissionInputs:
        """Resolve a quality-gate terminal outcome into the single-source inputs.

        OMN-13475 / OMN-13629: the gate paths (completed-pass,
        required_bar_missing, and gate-failed-escalation-exhausted) all funnel
        through here so the one ``_emit_terminal`` builder produces the single
        canonical ``ModelDelegationResult`` terminal from these identical values
        — no separate or duplicate terminal construction.
        """
        assert workflow.request is not None
        assert workflow.routing_decision is not None
        assert workflow.inference_model_used is not None

        # OMN-13355: pin the premium counterfactual so cost_savings_usd
        # (= counterfactual_cost_usd - cost_usd) is auditable. Only banked on the
        # completed/accepted path; failure paths bank no saving (None).
        premium_counterfactual = (
            build_premium_counterfactual(
                prompt_tokens=workflow.inference_prompt_tokens,
                completion_tokens=workflow.inference_completion_tokens,
            )
            if completed
            else None
        )

        history_dicts = tuple(
            attempt.model_dump(mode="json") for attempt in workflow.escalation_history
        )
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

        return TerminalEmissionInputs(
            completed=completed,
            correlation_id=result.correlation_id,
            task_type=workflow.request.task_type,
            model_used=workflow.inference_model_used,
            endpoint_url=workflow.routing_decision.endpoint_url,
            content=workflow.inference_content or "",
            quality_passed=completed,
            quality_score=result.quality_score,
            latency_ms=elapsed_ms,
            prompt_tokens=workflow.inference_prompt_tokens,
            completion_tokens=workflow.inference_completion_tokens,
            total_tokens=workflow.inference_total_tokens,
            fallback_to_claude=fallback_to_claude,
            failure_reason=failure_reason,
            tokens_to_compliance=tokens_to_compliance,
            compliance_attempts=compliance_attempts,
            cost_tier_name=workflow.current_tier_name or "",
            premium_counterfactual=premium_counterfactual,
            escalation_count=workflow.escalation_count,
            escalation_history=history_dicts,
            terminal_failure_reason=terminal_failure_reason,
            routing_tiers_hash=self._routing_tiers_hash(),
            escalation_config_hash=None,
            attempts_count=workflow.escalation_count + 1,
            model_name=workflow.routing_decision.selected_model,
            session_id=None,
            quality_gates_checked=quality_gates_checked,
            quality_gates_failed=[] if completed else list(result.failure_reasons),
            llm_call_id=workflow.inference_llm_call_id,
            context_pack_hash=workflow.context_pack_hash,
            # OMN-13535: metered spend banked on every prior attempted tier so the
            # terminal cost_usd reflects total spend, not just the final tier.
            prior_attempt_cost_usd=workflow.cumulative_attempt_cost_usd,
            prior_attempt_prompt_tokens=workflow.cumulative_attempt_prompt_tokens,
            prior_attempt_completion_tokens=workflow.cumulative_attempt_completion_tokens,
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
                self._advance(workflow, EnumDelegationState.EXECUTING)
            return []

        if workflow.state not in {
            EnumDelegationState.ROUTED,
            EnumDelegationState.EXECUTING,
        }:
            return []

        if workflow.state != next_state:
            self._advance(workflow, next_state)

        assert workflow.request is not None

        elapsed_ms = (time.monotonic_ns() - workflow.started_at_ns) // 1_000_000
        delegated_to = (
            workflow.invocation_command.target_ref
            if workflow.invocation_command is not None
            else "remote-agent"
        )
        content = self._render_lifecycle_content(lifecycle_event)
        failure_reason = lifecycle_event.error or ""

        completed = next_state is EnumDelegationState.COMPLETED
        # OMN-13396/OMN-13475: the remote-agent (A2A) lifecycle carries no token
        # counts and no serving tier — it is not a tier-routed LLM inference. The
        # single terminal builder still prices it through the same typed-tier-cost
        # model so the zero is PROVEN by the cost model (no_cost_model provenance)
        # rather than a silent hardcoded 0.0: an unset tier resolves to
        # no_cost_model and deterministically yields cash_cost_usd == 0.0.
        terminal_inputs = TerminalEmissionInputs(
            completed=completed,
            correlation_id=cid,
            task_type=workflow.request.task_type,
            model_used=delegated_to,
            endpoint_url=delegated_to,
            content=content,
            quality_passed=completed,
            quality_score=1.0 if completed else 0.0,
            latency_ms=elapsed_ms,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            fallback_to_claude=False,
            failure_reason=failure_reason,
            tokens_to_compliance=0,
            compliance_attempts=1,
            cost_tier_name=workflow.current_tier_name or "",
            premium_counterfactual=None,
            escalation_count=0,
            escalation_history=(),
            terminal_failure_reason=None,
            routing_tiers_hash=None,
            escalation_config_hash=None,
            attempts_count=1,
            model_name=delegated_to,
            session_id=None,
            quality_gates_checked=["agent-task-lifecycle"],
            quality_gates_failed=[failure_reason] if failure_reason else [],
            llm_call_id=lifecycle_event.remote_task_handle or "",
            context_pack_hash=workflow.context_pack_hash,
        )
        return self._emit_terminal(terminal_inputs)

    async def handle(self, payload: object) -> list[BaseModel]:
        """Route a workflow payload to its per-step FSM handler by payload type.

        OMN-13477 (W5): dispatch is driven by ``_PER_STEP_DISPATCH`` — a
        declarative ``payload model class -> per-step handler method`` table,
        one entry per ``handler_routing`` event_model the contract declares
        (``payload_type_match``). This replaces the prior hand-maintained
        isinstance ladder: each event model resolves to exactly one thin
        per-step handler, and there is no catch-all branch — an undeclared
        payload type fails closed (``ValueError``) rather than being silently
        swallowed by a fallthrough.
        """
        if isinstance(payload, ModelEventEnvelope) or hasattr(payload, "payload"):
            payload = payload.payload
        if isinstance(payload, dict):
            payload = self._coerce_payload_dict(payload)

        handler = self._resolve_per_step_handler(type(payload))
        if handler is None:
            msg = f"Unsupported delegation workflow payload: {type(payload).__name__}"
            raise ValueError(msg)
        return list(handler(payload))

    def _resolve_per_step_handler(
        self, payload_type: type
    ) -> Callable[[Any], list[Any]] | None:
        """Resolve the per-step FSM handler bound to ``payload_type``.

        The lookup keys on the most-derived declared payload class first
        (exact ``type`` match), then walks the declared base classes so a
        subclass of a declared event model still routes — without re-introducing
        an isinstance ladder. Returns ``None`` for an undeclared payload type so
        ``handle`` can fail closed.
        """
        method_name = _PER_STEP_DISPATCH.get(payload_type)
        if method_name is None:
            for declared_type, declared_method in _PER_STEP_DISPATCH.items():
                if issubclass(payload_type, declared_type):
                    method_name = declared_method
                    break
        if method_name is None:
            return None
        return cast("Callable[[Any], list[Any]]", getattr(self, method_name))

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
