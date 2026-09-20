# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-process local delegation dispatch (standalone CLI, no broker).

This is the default dispatch port used by ``HandlerDelegateSkill`` when no event
bus is wired — the ``onex delegate`` standalone CLI path on the local Mac. It is
NOT a transport port: it owns no HTTP/curl detail. Instead it composes the
canonical surfaces (OMN-13160):

  1. ROUTING AUTHORITY — ``resolve_delegation_backend`` resolves ``model_id`` +
     the COMPLETE ``endpoint_ref`` from the bifrost contract + installer overlay
     BEFORE the effect runs. No hand-rolled config loading lives here.
  2. CANONICAL EFFECT HANDLER — ``HandlerLlmDelegationCall`` executes exactly one
     LLM call. Its transport selects curl on ``local_macos_claude_hooks`` (the
     only LAN-safe transport on this Mac) and httpx elsewhere, and posts the
     resolved ``endpoint_ref`` VERBATIM (OMN-12815/OMN-13159).
  3. PROJECTION — the canonical ``HandlerProjectionDelegation`` materializes a
     ``delegation_events`` evidence row from the terminal event, run in-process
     against a projection target resolved from config (OMN-14015): the projection
     runtime binding overlay selects the backing store, defaulting to the local
     SQLite target when no overlay is configured (a truly bus-less CLI) and
     targeting the platform Postgres substrate when the overlay declares it. The
     deprecated DirectCurl port's bespoke sqlite write is replaced by the same
     canonical projection the bus runtime uses.

OMN-13849 — escalation loop + judge combine on the bus-less path:
  * On a quality-gate FAIL the port re-dispatches to the next eligible tier,
    mirroring the bus orchestrator's proven loop
    (``handler_delegation_workflow.handle_gate_result`` :1343-1400 /
    ``_decide_escalation`` :748-811): resolve the current tier via
    ``tier_for_backend``, compute ``next_eligible_tier(current, excluded,
    task_type=...)`` off the closed-set task-class ``tier_order``, re-resolve the
    escalated backend, and retry — bounded by ``escalation_policy.max_escalations``
    from ``task_class_contracts.v1.yaml``. Cheapest-first initial tier and the
    closed-set ``tier_order`` semantics (no unlisted tiers) are preserved.
  * For judge-combinable task classes the port runs the SAME ``HandlerJudgeAdequacy``
    EFFECT the bus quality-gate-intent handler runs
    (``handler_quality_gate_intent.handle_async`` :127-155) and threads the
    resolved ``judge_adequacy_score`` / ``judge_verdict`` into the gate reducer, so
    a good code answer can clear the 0.85 bar on the local path exactly as it does
    on the bus.
  * Every attempt's real metered cost (``result.actual_cost_usd``) is banked into
    the cumulative cost projected on the evidence row — a rejected metered tier's
    spend is never dropped (mirrors the bus ``_bank_attempt_spend``), and cost is
    never a hardcoded 0.0.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import multiprocessing
import queue
import sys
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumDelegationOutputRefusalReason,
    EnumQualityContractMode,
    ModelDelegationDeliverableEvidence,
    ModelDelegationOutputRefusal,
    ModelDelegationProvenance,
    ModelQualityGateInput,
)

from omnimarket.config import get_settings
from omnimarket.delegation.deliverable_extraction import (
    EnumDeliverableExtractionRefusal,
    canonical_deliverable_contract_sha256,
    extract_deliverable,
    resolve_task_class_deliverable_contract,
)
from omnimarket.delegation.reasoning_preamble import (
    EnumReasoningBoundaryRule,
    segment_reasoning_preamble,
)
from omnimarket.delegation.response_contract_conformance import (
    locate_schema_conforming_json,
    schema_violation_reasons,
)
from omnimarket.delegation.response_contract_instruction import (
    compose_system_prompt_with_response_contract,
)
from omnimarket.enums.enum_delegation_acceptance import (
    EnumDelegationAcceptanceDecision,
    EnumDelegationAcceptanceReason,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.delegation_judge_verdict import EnumDelegationJudgeVerdict
from omnimarket.inference.protocol_config import apply_inference_protocol
from omnimarket.inference.provider_finish_reason import (
    EnumProviderFinishReason,
    is_truncated_by_output_budget,
)
from omnimarket.local_deployment.tenant_identity import (
    ensure_install_identity_mirrored,
    resolve_local_deployment_tenant_id,
)

# The reducer (``delta``) returns the omnimarket wire result DTO (it carries the
# P1 deterministic-acceptance evidence fields not yet promoted to core), so the
# port annotates against that surface rather than the core re-export.
from omnimarket.models.delegation.local_credential_refusal import (
    EnumLocalCredentialRefusalReason,
)
from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    ModelQualityGateResult,
    ModelQualityRuleEvaluation,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.evidence_db_resolution import (
    resolve_local_delegation_evidence_db,
)

# OMN-13849: the SAME required-bar authority the bus orchestrator applies
# (``handler_delegation_workflow.handle_gate_result`` :1240-1253). The quality-gate
# reducer's ``result.passed`` is NOT the whole acceptance decision for a verifiable
# task class: the reducer returns ``passed=True`` for a code answer that clears the
# deterministic floor even at a graded score below the class ``required_bar`` (e.g.
# code_generation ~0.733 < 0.85). Acceptance = ``passed`` AND ``score >= required_bar``
# AND not a deterministic-floor rejection — resolved from the task-class contract.
from omnimarket.nodes.node_delegation_orchestrator.quality_bar_authority import (
    RequiredBarAuthorityError,
    resolve_required_bar_authority,
)

# OMN-13849: the SAME required-bar authority the bus orchestrator applies
# (``handler_delegation_workflow.handle_gate_result`` :1240-1253). The quality-gate
# reducer's ``result.passed`` is NOT the whole acceptance decision for a verifiable
# task class: the reducer returns ``passed=True`` for a code answer that clears the
# deterministic floor even at a graded score below the class ``required_bar`` (e.g.
# code_generation ~0.733 < 0.85). Acceptance = ``passed`` AND ``score >= required_bar``
# AND not a deterministic-floor rejection — resolved from the task-class contract.
# Canonical quality-gate reducer (OMN-13597): the SAME gate the bus path runs.
# ``delta`` is the pure reducer; ``resolve_task_class_dod_checks`` is the routing
# authority's public DoD resolver. Composing both here makes the local CLI path
# run the real gate instead of recording a hardcoded PASS — a refusal or empty
# answer now projects ``quality_gate_passed=false``.
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as evaluate_quality_gate,
)

# OMN-13849: the SAME judge EFFECT + combinable task-class set the bus
# quality-gate-intent handler uses. Reusing both (not re-declaring them) keeps the
# local path in parity with the bus path — a good code answer clears the bar the
# same way on both.
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    JUDGE_COMBINABLE_TASK_TYPES,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.judge.handler_judge_adequacy import (
    HandlerJudgeAdequacy,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    backend_id_for_tier,
    credential_withheld_rung,
    first_eligible_tier,
    is_free_tier,
    measure_grounding_input_tokens,
    next_eligible_tier,
    resolve_backend_grounding_budget,
    resolve_task_class_dod_checks,
    resolve_task_class_max_escalations,
    resolve_task_class_response_contract,
    shipped_house_credential_refs,
    sibling_backend_available_in_tier,
    tier_for_backend,
    tier_max_retries,
)

# Import the canonical effect via its public package surface (not its internal
# models package) so this composition stays on the node boundary (OMN-13160).
from omnimarket.nodes.node_llm_delegation_call_effect import (
    HandlerLlmDelegationCall,
    ModelLlmDelegationCallRequest,
    ModelLlmDelegationCallResult,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.protocol_database import DatabaseAdapter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from omnimarket.projection.tenant_isolation import (
    TenantContextMissingError,
)
from omnimarket.routing.customer_key_terminus import (
    EnumDelegationSurface,
    enforce_customer_key_terminus,
)
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_effective_max_tokens,
    resolve_timeout_seconds,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend as _resolve_delegation_backend_uncustomized,
)
from omnimarket.routing.local_byok_route import substitute_local_byok_route
from omnimarket.routing.roi_overlay import (
    ModelRoutingRoiOverlay,
    resolve_roi_overlay,
)

logger = logging.getLogger(__name__)


_RESERVED_PROVIDER_REQUEST_KEYS = frozenset(
    # OMN-15482: ``response_format`` joins the reserved set. It is now a
    # first-class caller-supplied wire parameter threaded from
    # ``ModelDelegateSkillRequest.response_format``; letting an inference
    # profile also write it through ``provider_request_options`` would create a
    # second, silently-winning path to the same key. No shipped profile sets it
    # today, so this is a fail-loud ratchet, not a behavior change.
    {"model", "messages", "max_tokens", "temperature", "response_format"}
)

# OMN-15482: the effect model's OWN declared temperature default, read off the
# model rather than restated as a literal here. ``LocalDelegationDispatchPort``
# previously omitted ``temperature`` entirely and inherited this value; a
# caller that supplies no temperature must keep getting exactly it, and a change
# to the model's default must move both together rather than silently diverging.
_DEFAULT_CALL_TEMPERATURE: float = float(
    cast(float, ModelLlmDelegationCallRequest.model_fields["temperature"].default)
)

# Task-type system prompts carried forward from the deprecated DirectCurl port so
# the local CLI delegation keeps producing the same on-task system framing.
_TASK_TYPE_SYSTEM_PROMPTS: dict[str, str] = {
    "test": "You are an expert test engineer. Write comprehensive, well-structured tests.",
    "document": "You are a technical writer. Write clear, accurate documentation.",
    "research": "You are a senior software engineer. Analyze the topic thoroughly and provide a detailed, well-structured response.",
    "code_generation": "You are an expert software engineer. Write clean, production-quality code.",
    "code_review": "You are a code review assistant. Identify bugs, style violations, and architectural issues in the provided code. Be specific and actionable.",
    "refactor": "You are an expert software engineer specializing in refactoring. Improve code quality while preserving behavior.",
    "reasoning": "You are an expert analyst. Think step-by-step and provide well-reasoned conclusions.",
    "review": "You are a senior code reviewer. Provide thorough, actionable feedback.",
}

_DELEGATION_EVENTS_TABLE = "delegation_events"

# OMN-13849: the escalation budget the local path uses when the task class declares
# no ``escalation_policy.max_escalations``. Mirrors the bus orchestrator's
# ``handle_gate_result`` default (``max_escalation_attempts=2``) so a task class
# without a contract-declared budget still escalates the same bounded number of
# times on both paths.
_DEFAULT_MAX_ESCALATIONS = 2

# OMN-13943: failure classes that must NOT trigger an up-tier escalation retry.
# Mirrors the bus orchestrator's ``_should_escalate_inference_error`` posture
# (retry unless PROVEN non-retryable) but classifies on the effect result's typed
# ``failure_class`` instead of raw error text, since the bus-less local port has
# that structured field available. PROVIDER_AUTH_FAILED is excluded because
# re-issuing the same prompt will not turn a bad credential into a good one on
# the SAME backend, and silently escalating past an auth failure would mask a
# real credential-config bug as a transient one. INVALID_JSON is excluded
# because a structurally malformed response is a provider/contract defect, not a
# transient condition a retry on a different tier is likely to fix. Every other
# class — RATE_LIMITED, TIMEOUT, MODEL_UNAVAILABLE, CONTEXT_TOO_LARGE,
# PRICING_UNKNOWN, UNKNOWN — is retryable, matching the bus's default-retry
# posture. This is exactly the classification OMN-13943 requires: a GLM 429
# (RATE_LIMITED) must fall through to the next tier instead of terminating.
_NON_RETRYABLE_TRANSPORT_FAILURE_CLASSES: frozenset[EnumDelegationFailureClass] = (
    frozenset(
        {
            EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
            # OMN-18696: a declared credential with no resolvable value. Excluded
            # on the same reasoning as PROVIDER_AUTH_FAILED and one step
            # stronger: no call was made at all, so there is not even a
            # transient provider condition for a retry to outlast. Escalating
            # past it would climb the whole ladder and then report a generic
            # failure, hiding a one-line configuration fix behind an apparent
            # capacity problem.
            EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING,
            EnumDelegationFailureClass.INVALID_JSON,
        }
    )
)


def _declared_required_bar(task_type: str) -> float | None:
    """The task class's declared bar, or ``None`` when it declares none.

    ``_is_quality_accepted`` already treats an unresolvable bar as "the reducer
    verdict is the authority"; this reports the same fact on the receipt rather
    than printing a number the contract never declared (OMN-18379).
    """
    try:
        return resolve_required_bar_authority(task_type=task_type).required_bar
    except RequiredBarAuthorityError:
        return None


def _named_blocking_vetoes(
    rule_evaluations: tuple[ModelQualityRuleEvaluation, ...],
) -> str:
    """Render every blocking rule that failed, with its own failure text.

    Empty when no rule evaluation can be pointed at — a caller must then report
    the refusal without naming a decider rather than inventing one.
    """
    return "; ".join(
        f"{evaluation.rule}: {evaluation.detail}"
        if evaluation.detail
        else evaluation.rule
        for evaluation in rule_evaluations
        if not evaluation.passed and evaluation.enforcement == "blocking"
    )


def derive_attempt_acceptance(
    *,
    quality_passed: bool,
    pre_filter_rejected: bool,
    gate_passed: bool,
    judge_unavailable_floor: bool,
    quality_score: float,
    required_bar: float | None,
    rule_evaluations: tuple[ModelQualityRuleEvaluation, ...],
) -> tuple[EnumDelegationAcceptanceDecision, EnumDelegationAcceptanceReason, str]:
    """Derive the typed accept/climb verdict and a reason that names the decider.

    OMN-18379. This path recorded a BINARY reason — ``quality_bar_met`` when the
    attempt was accepted and ``score_below_required_bar`` for every refusal,
    whatever the score. Run ``43d269f5`` (``document`` class, 2026-09-14) was
    therefore refused three times with ``score_below_required_bar`` at
    ``quality_score: 0.9`` against a declared bar of ``0.8`` — a label that
    contradicts its own printed numbers, and one that sent the operator looking
    for a weak model when the real decider was the blocking rule ``accurate``
    firing on a word inside a leaked reasoning scratchpad.

    The bus orchestrator already had the three-way split (OMN-15464, typed in
    OMN-16932). This brings the bus-less local path to parity and goes one step
    further: when the refusal is attributable to named blocking rules, the
    reason IS ``heuristic_veto`` and the detail names each rule with its own
    failure text, which for a phrase-scanning rule carries the matched phrase
    and its offset.

    Precedence matches the acceptance expression the bus path evaluates: the
    deterministic floor short-circuits, then the acceptance criteria, then the
    numeric bar.

    Args:
        required_bar: The task class's declared bar, or ``None`` when the class
            declares none. ``None`` is reported as ``required_bar=undeclared``
            rather than substituted with a number, because a bar nobody
            declared cannot be the thing that decided.

    Returns:
        ``(decision, reason, detail)``. ``detail`` is human-readable and always
        states the score and the bar, so no consumer has to infer the
        comparison from the label.
    """
    if required_bar is None:
        detail = (
            f"actual_score={quality_score:.3f} required_bar=undeclared "
            f"score_vs_bar=no_bar_declared"
        )
    else:
        detail = (
            f"actual_score={quality_score:.3f} required_bar={required_bar:.3f} "
            f"score_vs_bar="
            f"{'below_bar' if quality_score < required_bar else 'at_or_above_bar'}"
        )
    if quality_passed:
        return (
            EnumDelegationAcceptanceDecision.ACCEPT,
            EnumDelegationAcceptanceReason.QUALITY_BAR_MET,
            detail,
        )
    named_vetoes = _named_blocking_vetoes(rule_evaluations)
    if pre_filter_rejected:
        # OMN-18278: name the decider here too. The reason CLASS was already
        # honest, but the detail was score-shaped and nothing else -- a
        # truncation refusal printed `actual_score=0.000 required_bar=0.800
        # score_vs_bar=below_bar`, which reads as "the model was weak" when the
        # real fact is that the provider cut the response off mid-generation.
        # That is the same misdirection OMN-18379 removed from the branch below;
        # it survived on this one because the floor short-circuits ahead of it.
        return (
            EnumDelegationAcceptanceDecision.CLIMB,
            EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED,
            f"{detail} vetoed_by={named_vetoes}" if named_vetoes else detail,
        )
    if not gate_passed:
        if named_vetoes:
            return (
                EnumDelegationAcceptanceDecision.CLIMB,
                EnumDelegationAcceptanceReason.HEURISTIC_VETO,
                f"{detail} vetoed_by={named_vetoes}",
            )
        # The gate refused and named no blocking rule. That is a real answer,
        # not a reason to invent one: say the criteria failed and leave the
        # veto label for the case where a rule can actually be pointed at.
        return (
            EnumDelegationAcceptanceDecision.CLIMB,
            EnumDelegationAcceptanceReason.ACCEPTANCE_CRITERIA_FAILED,
            detail,
        )
    if judge_unavailable_floor:
        # Unreachable while ``quality_passed`` is the caller's own verdict over
        # the same booleans, but stated so precedence is readable here.
        return (
            EnumDelegationAcceptanceDecision.ACCEPT,
            EnumDelegationAcceptanceReason.JUDGE_UNAVAILABLE_DETERMINISTIC_FLOOR,
            detail,
        )
    return (
        EnumDelegationAcceptanceDecision.CLIMB,
        EnumDelegationAcceptanceReason.SCORE_BELOW_REQUIRED_BAR,
        detail,
    )


def _terminal_artifact(
    best_content: str, last_result: ModelLlmDelegationCallResult
) -> str:
    """The artifact a FAILED terminal hands back, never a truncated fragment.

    ``best_content`` is the highest-scoring non-empty draft across every attempt
    (OMN-14220), so a false-rejected but correct earlier authorship survives
    escalation. When no attempt produced one, the last attempt's own text is the
    only candidate left — unless the provider said it cut that text off at the
    output-token budget (OMN-18278), in which case there is no artifact and the
    honest answer is the empty one. A caller can tell the difference from the
    failure reasons, which name the truncation.
    """
    if best_content:
        return best_content
    if is_truncated_by_output_budget(last_result.finish_reason):
        return ""
    return last_result.content or ""


def _routing_tier_name(backend: ModelResolvedDelegationBackend) -> str:
    """Return the routing-authority tier name for ``backend`` (OMN-15803).

    ``ModelResolvedDelegationBackend.tier`` is populated from the bifrost
    contract's own descriptive ``tier:`` field (e.g. ``cloud-gemini-pro``
    declares ``tier: frontier_api`` in ``bifrost_delegation.yaml``) — a
    DIFFERENT vocabulary than the routing_tiers.yaml ``tier_order`` names
    (``local``/``cheap_cloud``/``claude``) the task-class contract declares.
    Every attempts[]/log/wire-metadata surface must report the ROUTING tier —
    the tier_order member actually walked — never the raw bifrost label, which
    is not even guaranteed to be a member of the task class's declared
    ``tier_order``. Reading ``backend.tier`` directly produced a receipt
    showing ``tier="frontier_api"`` even though the resolver had correctly
    walked ``cheap_cloud -> claude``, and fed the same wrong label into
    ``ModelLlmDelegationCallRequest.model_tier``, which
    ``handler_llm_delegation_call._get_tier_price_per_1m`` /
    ``_FALLBACK_PRICE_PER_1M`` key on routing_tiers.yaml vocabulary — silently
    mispricing the call. ``tier_for_backend`` is the single parsing path
    (``handler_delegation_routing``); the bifrost field is the fallback ONLY
    for a backend not declared in routing_tiers.yaml at all (should not occur
    for a resolved backend).
    """
    return tier_for_backend(backend.backend_id) or backend.tier


def _is_retryable_transport_failure(
    failure_class: EnumDelegationFailureClass | None,
) -> bool:
    """Return whether a transport/timeout failure should trigger up-tier escalation.

    ``None`` (a failure result carrying no typed classification) is treated as
    retryable, matching the bus's default-retry posture of escalating unless the
    failure is PROVEN non-retryable.
    """
    return failure_class not in _NON_RETRYABLE_TRANSPORT_FAILURE_CLASSES


# OMN-13597: hard ceiling buffer (seconds) added to the contract-resolved
# per-backend transport timeout when bounding the blocking effect call. Covers
# the synchronous health probe that precedes the LLM POST so a stalled connect
# can never hang the local CLI past ``transport_timeout + buffer``.
_DISPATCH_TIMEOUT_BUFFER_SECONDS = 10.0
_EFFECT_PROCESS_POLL_INTERVAL_SECONDS = 0.05
_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS = 1.0

# OMN-14883: the effect child's INTERPRETER BOOT is bounded separately from the
# endpoint's transport budget above.
#
# A ``spawn`` child (OMN-13842 — mandatory on macOS, where ``fork`` after the
# objc runtime is live aborts the child) is a fresh interpreter: before it can
# call the pickled effect handler it must re-import that handler's module and the
# whole transitive package tree behind it. Measured cost of that import alone:
# ~18s on macOS and ~80s on the .201 gate-runner container (user CPU ~7.9s on
# both — the delta is pure I/O off the container's ``/data`` mount). None of it
# touches the endpoint.
#
# Charging that boot to ``timeout_seconds + _DISPATCH_TIMEOUT_BUFFER_SECONDS``
# (as small as 10.5s for a backend declaring ``timeout_ms: 500``) killed healthy
# children mid-import and projected a canonical TIMEOUT with
# ``endpoint_healthy=False`` — a verdict on an endpoint that was never contacted.
# The boot budget below therefore exists ONLY to bound a wedged interpreter; it
# is never an endpoint verdict, and a boot overrun raises an infrastructure
# RuntimeError rather than the TimeoutError the caller projects as a transport
# failure.
#
# The budget is derived from a MEASURED in-run calibration rather than a constant
# sized for one host: every child that reaches readiness reports how long its own
# boot took, and subsequent children on the same host scale their budget off that
# observation. The ceiling applies only until the first observation lands (and as
# an absolute hang guard thereafter); the floor keeps one fast, warm-cache sample
# from arming a budget the next cold boot blows.
_EFFECT_CHILD_BOOT_CEILING_SECONDS = 300.0
_EFFECT_CHILD_BOOT_FLOOR_SECONDS = 30.0
_EFFECT_CHILD_BOOT_SAFETY_FACTOR = 4.0

# Slowest child boot observed in THIS process, in seconds. ``None`` until the
# first child reports readiness.
_observed_child_boot_seconds: float | None = None

type _EffectHandler = Callable[
    [ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult
]
type _EffectWorkerMessage = (
    tuple[Literal["ready"]]
    | tuple[Literal["ok"], ModelLlmDelegationCallResult]
    | tuple[Literal["error"], str, str]
)


def _resolve_child_boot_budget_seconds(*, observed_boot_seconds: float | None) -> float:
    """Bound the effect child's interpreter boot from a measured observation.

    ``observed_boot_seconds`` is the slowest boot this process has actually seen
    on this host. ``None`` (nothing measured yet) falls back to the hang-guard
    ceiling, which is the only un-calibrated value in the path.
    """
    if observed_boot_seconds is None:
        return _EFFECT_CHILD_BOOT_CEILING_SECONDS
    scaled = observed_boot_seconds * _EFFECT_CHILD_BOOT_SAFETY_FACTOR
    return min(
        _EFFECT_CHILD_BOOT_CEILING_SECONDS,
        max(_EFFECT_CHILD_BOOT_FLOOR_SECONDS, scaled),
    )


def _record_child_boot_observation(boot_seconds: float) -> None:
    """Feed a measured boot forward as the calibration for the next child."""
    global _observed_child_boot_seconds
    if (
        _observed_child_boot_seconds is None
        or boot_seconds > _observed_child_boot_seconds
    ):
        _observed_child_boot_seconds = boot_seconds


def _resolve_effect_process_context() -> Any:
    """Resolve the multiprocessing start method for the effect child process.

    OMN-13842: the delegation effect runs a real LLM call (health probe + curl /
    httpx POST) that touches macOS system frameworks (Foundation / CoreFoundation
    proxy resolution, TLS). Those initialize the Objective-C runtime in the parent
    process. Starting the child with ``fork`` then executing any objc call in the
    child aborts the child with SIGABRT (``objc[...]: +[... initialize] may have
    been in progress in another thread when fork() was called ... Crashing
    instead``) — exit code ``-6``, zero LLM output, and the local ``onex delegate``
    CLI reports ``delegation effect process exited without returning a result``.

    ``fork`` is only unsafe *after* the objc runtime is live, which is exactly the
    delegation effect's steady state on ``local_macos_claude_hooks``. ``spawn``
    starts a clean interpreter with no inherited objc state, so the child is
    fork-safe. It requires the worker target + args to be picklable (they are:
    a module-level worker function, a pickleable effect handler, and a spawn-
    context ``Queue``). Use ``spawn`` on macOS; keep the cheaper ``fork`` (with a
    ``spawn`` fallback for platforms that lack it) elsewhere, where objc
    fork-safety does not apply.
    """
    if sys.platform == "darwin":
        return multiprocessing.get_context("spawn")
    try:
        return multiprocessing.get_context("fork")
    except ValueError:
        return multiprocessing.get_context("spawn")


def _effect_handler_worker(
    effect_handler: _EffectHandler,
    request: ModelLlmDelegationCallRequest,
    result_queue: Any,
) -> None:
    """Signal readiness, then run the sync effect and return exactly one result.

    OMN-14883: the ``ready`` message is the child's FIRST act. Reaching this
    function already proves the ``spawn`` round-trip completed — the interpreter
    booted, the target module and the pickled handler's module imported, and the
    handler unpickled. Everything after it is the effect call itself, so the
    parent can bound the two phases on their own budgets instead of charging a
    slow package import to the endpoint's transport timeout.
    """
    result_queue.put(("ready",))
    try:
        result = effect_handler(request)
        result_queue.put(("ok", result))
    except Exception as exc:
        result_queue.put(("error", type(exc).__name__, str(exc)))


def _read_effect_worker_message(result_queue: Any) -> _EffectWorkerMessage | None:
    try:
        return cast(_EffectWorkerMessage, result_queue.get_nowait())
    except queue.Empty:
        return None


def _terminate_effect_process(process: Any) -> None:
    if not process.is_alive():
        process.join(timeout=0)
        return
    process.terminate()
    process.join(timeout=_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS)


async def _run_effect_handler_with_killable_timeout(
    effect_handler: _EffectHandler,
    request: ModelLlmDelegationCallRequest,
    *,
    timeout_seconds: float,
) -> ModelLlmDelegationCallResult:
    """Run the blocking sync effect behind a process boundary with a hard kill.

    OMN-14883: the child runs in two phases on two separate budgets.

    * BOOT — from ``process.start()` until the child's ``ready`` message. This is
      interpreter startup plus the ``spawn`` re-import of the handler's module
      tree; it never touches the endpoint. Bounded by
      ``_resolve_child_boot_budget_seconds`` (calibrated from the slowest boot
      measured in this process), and an overrun raises an infrastructure
      ``RuntimeError`` — never the ``TimeoutError`` the caller projects as a
      transport failure against the endpoint.
    * CALL — from ``ready`` onward, bounded by ``timeout_seconds``: the
      contract-resolved transport budget the caller passed in. Unchanged
      semantics, now measuring only what it claims to measure.
    """
    context: Any = _resolve_effect_process_context()
    result_queue = context.Queue()
    process = context.Process(
        target=_effect_handler_worker,
        args=(effect_handler, request, result_queue),
        daemon=True,
    )
    boot_started = time.monotonic()
    process.start()

    boot_budget_seconds = _resolve_child_boot_budget_seconds(
        observed_boot_seconds=_observed_child_boot_seconds
    )
    child_ready = False
    deadline = boot_started + boot_budget_seconds
    try:
        while True:
            message = _read_effect_worker_message(result_queue)
            if message is not None:
                if message[0] == "ready":
                    boot_seconds = time.monotonic() - boot_started
                    _record_child_boot_observation(boot_seconds)
                    logger.debug(
                        "LocalDelegationDispatch: effect child booted in %.2fs "
                        "(boot budget %.0fs); starting the %.1fs endpoint budget",
                        boot_seconds,
                        boot_budget_seconds,
                        timeout_seconds,
                    )
                    child_ready = True
                    deadline = time.monotonic() + timeout_seconds
                    continue

                process.join(timeout=_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS)
                if message[0] == "ok":
                    return message[1]
                raise RuntimeError(f"{message[1]}: {message[2]}")

            if not process.is_alive():
                process.join(timeout=_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS)
                while (
                    message := _read_effect_worker_message(result_queue)
                ) is not None:
                    if message[0] == "ready":
                        continue
                    if message[0] == "ok":
                        return message[1]
                    raise RuntimeError(f"{message[1]}: {message[2]}")
                raise RuntimeError(
                    "delegation effect process exited without returning a result "
                    f"(exitcode={process.exitcode})"
                )

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _terminate_effect_process(process)
                if child_ready:
                    raise TimeoutError
                # Boot overrun: the child never became ready, so no request ever
                # left this host. Fail as infrastructure, never as an endpoint
                # verdict — projecting TIMEOUT here would blame an endpoint that
                # was never contacted.
                raise RuntimeError(
                    "delegation effect process did not become ready within "
                    f"{boot_budget_seconds:.0f}s (interpreter boot / module "
                    "import, not an endpoint failure)"
                )
            await asyncio.sleep(min(_EFFECT_PROCESS_POLL_INTERVAL_SECONDS, remaining))
    finally:
        if process.is_alive():
            _terminate_effect_process(process)
        result_queue.close()
        result_queue.join_thread()


def resolve_delegation_backend(
    task_type: str, *, backend_id: str | None = None
) -> ModelResolvedDelegationBackend:
    """Resolve a backend for the LOCAL path, honouring a locally registered BYOK key.

    OMN-18694. Every backend resolution in this module goes through here rather
    than calling the routing authority directly, so the BYOK substitution cannot
    be missed by a call site added later — there are five today and the
    guarantee must not depend on remembering all of them.

    The wrapper is deliberately thin and total: it resolves exactly as before,
    then applies :func:`substitute_local_byok_route`, which is a no-op unless
    the resolved rung carries a HOUSE ``secret_ref`` AND the customer has
    registered their own key for that provider. A free local rung (no
    ``secret_ref``) is returned untouched, so cheapest-first is unchanged.

    Errors propagate verbatim: ``resolve_delegation_backend``'s fail-closed
    ``RuntimeError`` is what the pin/tier branches above are written against.
    """
    resolved = _resolve_delegation_backend_uncustomized(
        task_type, backend_id=backend_id
    )
    return substitute_local_byok_route(resolved)


class LocalDelegationDispatchPort:
    """Resolve routing, run the canonical effect, and project evidence in-process.

    Default port when no event_bus is provided (local ``onex delegate``). Owns no
    transport detail — the effect handler selects curl/httpx by runtime profile.

    OMN-13849: dispatch runs an in-process escalation loop. On a quality-gate FAIL
    it re-dispatches to the next eligible tier (bounded by the task-class
    ``max_escalations``), and it threads an LLM-judge adequacy score into the gate
    for judge-combinable task classes — parity with the bus orchestrator.
    """

    def __init__(
        self,
        *,
        effect_handler: _EffectHandler | None = None,
        projection_handler: HandlerProjectionDelegation | None = None,
        evidence_db: DatabaseAdapter | None = None,
        evidence_db_path: Path | None = None,
        effect_process_boundary: bool = True,
        judge: HandlerJudgeAdequacy | None = None,
        roi_db: DatabaseAdapter | None = None,
        roi_overlay_reader: Callable[[str], ModelRoutingRoiOverlay | None]
        | None = None,
    ) -> None:
        self._effect_handler = effect_handler or HandlerLlmDelegationCall()
        self._projection_handler = projection_handler or HandlerProjectionDelegation()
        # OMN-14015: the evidence DB target is no longer a hardcoded SQLite default.
        # Precedence: an explicitly injected adapter (composition root / tests) wins;
        # then an explicit sqlite path override (kept for the many tests that pin a
        # tmp_path DB); otherwise resolve the target from config
        # (``resolve_local_delegation_evidence_db`` reads the projection runtime
        # binding overlay, defaulting to the canonical local SQLite target when no
        # overlay is configured — byte-identical to the prior hardcoded default, so
        # golden replays are unaffected).
        self._evidence_db: DatabaseAdapter
        if evidence_db is not None:
            self._evidence_db = evidence_db
        elif evidence_db_path is not None:
            self._evidence_db = SqliteDatabaseAdapter(evidence_db_path)
        else:
            self._evidence_db = resolve_local_delegation_evidence_db()
        self._effect_process_boundary = effect_process_boundary
        # The judge wraps the canonical inference bridge; inject a fake/replay
        # bridge in tests to avoid (or replay) the network call. Same surface the
        # bus quality-gate-intent handler injects (OMN-13470/OMN-13849).
        self._judge = judge if judge is not None else HandlerJudgeAdequacy()
        # OMN-14001 — the first closed platform learning loop. The ROI overlay is
        # read from the ``context_roi_scores`` projection and threaded (as a pure
        # input) into the routing authority so a proven-failing tier is demoted
        # from the routing decision. ``roi_db`` is the projection adapter that
        # carries ``context_roi_scores`` — DISTINCT from ``_evidence_db`` (the local
        # SQLite ``delegation_events`` sink, which does NOT hold that table). When
        # neither ``roi_db`` nor a custom ``roi_overlay_reader`` is provided the
        # reader is a no-op returning None, so the loop is fail-OPEN: the local CLI
        # degrades to the static tier order off-network or when the projection is
        # unreachable, and never blocks on a telemetry read.
        self._roi_db = roi_db
        self._roi_overlay_reader = (
            roi_overlay_reader or self._default_roi_overlay_reader
        )

    def _default_roi_overlay_reader(
        self, task_type: str
    ) -> ModelRoutingRoiOverlay | None:
        """Resolve the ROI overlay from ``roi_db`` — fail-OPEN on outage (OMN-14001).

        Returns None when no ROI projection adapter was injected (the local
        default), or on a read error, so a captured-outcome read never breaks a
        live delegation. When a ``roi_db`` carrying ``context_roi_scores`` IS
        provided (the runtime / live-proof wiring), the real per-tier success rates
        drive tier suppression. ``tier_for_backend`` maps each row's
        ``endpoint_ref`` to its routing tier.

        OMN-16092 — ``TenantContextMissingError`` PROPAGATES instead of degrading.
        ``context_roi_scores`` is RLS-covered: a read with no ``app.tenant_id``
        tenant context comes back EMPTY rather than erroring, so absorbing that
        refusal here would silently route on a blinded read and be
        indistinguishable from "no ROI data yet". This handler's fail-open remit
        is telemetry OUTAGES; a missing tenant seam is a correctness defect and
        must surface.
        """
        if self._roi_db is None:
            return None
        try:
            return resolve_roi_overlay(
                self._roi_db,
                task_type=task_type,
                tier_of_endpoint=tier_for_backend,
            )
        except TenantContextMissingError:
            logger.error(
                "ROI overlay refused for task_type=%s: no tenant context for the "
                "RLS-covered projection read; failing the delegation closed rather "
                "than degrading to static tiers on a blinded read (OMN-16092)",
                task_type,
            )
            raise
        except Exception:
            logger.warning(
                "ROI overlay resolution failed for task_type=%s; static tiers",
                task_type,
                exc_info=True,
            )
            return None

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int | None,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        execution_timeout_seconds: int,
        terminal_delivery_margin_seconds: int,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
        provenance: ModelDelegationProvenance | None = None,
        backend_id: str | None = None,
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> dict[str, object]:
        if execution_timeout_seconds < 1:
            raise ValueError("execution_timeout_seconds must be positive")
        if terminal_delivery_margin_seconds < 1:
            raise ValueError("terminal_delivery_margin_seconds must be positive")
        # OMN-15156: an optional caller-supplied backend PIN. ``None`` (the
        # default) preserves the exact pre-existing cheapest-first task_type +
        # tier_order resolution — see ``_resolve_initial_backend``. A non-None
        # value bypasses tier_order selection entirely for the INITIAL attempt
        # only; it does NOT change escalation behavior — see the
        # ``_resolve_initial_backend`` docstring's "Escalation-interaction note"
        # for the observed pin+failure interaction (a transport failure on the
        # pinned backend still excludes its WHOLE tier, not just that backend).
        #
        # OMN-14001 — read the captured-outcome ROI overlay ONCE per delegation
        # (fail-open None when no ROI projection is wired). Threaded as a pure input
        # into the routing authority so a tier proven to fail in ``context_roi_scores``
        # is demoted from BOTH the initial resolution and every escalation hop, with
        # the same overlay across the whole dispatch for a deterministic decision.
        roi_overlay = self._roi_overlay_reader(task_type)
        # OMN-14058 (OPERATOR-ACCEPTED INTERIM), refined by OMN-14349: prefer a
        # caller-supplied verified tenant_id (would only be non-None if something
        # upstream of this genuinely bus-less path stamped one -- structurally
        # rare, but never override a real value with the local env-var interim).
        #
        # OMN-18699 closes the bottom of that chain. It used to end in `or None`,
        # and the evidence writer then substituted HOUSE_TENANT_SLUG -- so every
        # local row on an un-initialised install was recorded as OmniNode's. On a
        # local path the customer's machine IS the deployment, so the fallback is
        # now this install's OWN minted identity, and an install that has never
        # minted one is a typed REFUSAL rather than a run under the house tenant.
        # The refusal is raised here, before any provider is called, so a run
        # that cannot be attributed does no work and spends nothing.
        resolved_tenant_id = resolve_local_deployment_tenant_id(
            tenant_id or (get_settings().onex_tenant_id or None)
        )

        # 1. ROUTING AUTHORITY — resolve the INITIAL (cheapest-first) backend.
        #    Cheapest-first among tiers NOT ROI-suppressed; escalation only advances
        #    UP the closed-set task-class tier_order (OMN-13140/OMN-13849/OMN-14001).
        #    OMN-15156: a caller-supplied backend_id pin bypasses this cheapest-first
        #    selection for the INITIAL attempt only — see _resolve_initial_backend.
        backend = self._resolve_initial_backend(
            task_type, roi_overlay=roi_overlay, backend_id=backend_id
        )

        # Escalation budget from the task-class contract escalation_policy
        # (OMN-13849). None -> the class declares no budget; fall back to the bus
        # orchestrator's default so both paths escalate the same bounded count.
        max_escalations = resolve_task_class_max_escalations(task_type)
        if max_escalations is None:
            max_escalations = _DEFAULT_MAX_ESCALATIONS

        # Tiers already attempted (excluded from re-selection), mirroring the bus
        # ``excluded_tiers`` set threaded into ``next_eligible_tier``.
        excluded_tiers: set[str] = set()
        # OMN-15803: every backend_id already attempted anywhere in THIS
        # dispatch (transport failure OR quality-gate FAIL), threaded into
        # every escalation hop's ``next_eligible_tier`` call — see
        # ``_resolve_next_backend``'s docstring. Without this, two routing
        # tiers that declare the same concrete backend for a task type made
        # escalation a functional no-op (identical backend+model re-attempted).
        excluded_backend_refs: set[str] = set()
        # Cumulative metered spend banked across every attempted tier (OMN-13849):
        # a rejected metered tier's real cost is never dropped (bus
        # ``_bank_attempt_spend`` parity). Projected as the row's cost_usd.
        cumulative_cost_usd = Decimal("0")
        cumulative_savings_usd = Decimal("0")
        attempts: list[dict[str, object]] = []
        escalation_count = 0
        # OMN-14220: best authored artifact seen across attempts (highest gate score,
        # non-empty). On a terminal FAILURE the loop below used to return only the
        # LAST attempt's ``result.content`` — which is empty when the final hop is a
        # transport failure (e.g. a 429), so a caller received NOTHING even though an
        # earlier tier authored a correct artifact that merely lost the (false-)
        # rejecting gate. The rejected content was logged (OMN-14004) but never
        # surfaced on the terminal payload. Track the best artifact and return it on
        # failure so a successful authorship is never silently discarded.
        best_content: str = ""
        best_content_score: float = -1.0
        # OMN-14234 (retry-local / best-of-N): per-free-tier count of $0 re-draft
        # retries already issued. The local coder is non-deterministic (a trivial
        # refactor scored 0.8/0.64/1.0 at the 0.85 bar), so ~2/3 of first drafts
        # escalated to a PAID tier despite local inference being $0. Before
        # escalating off a FREE tier we retry the SAME backend up to its
        # contract-declared ``max_retries`` budget (routing_tiers.yaml; local=2 ->
        # 3 total $0 drafts), lifting the $0 pass-rate before paying. Keyed per tier
        # so each free tier on the ladder gets its own best-of-N budget.
        local_retry_counts: dict[str, int] = {}

        while True:
            # OMN-17434: the single point on this bus-less port where a
            # backend becomes EXECUTABLE — the initial resolution (pinned or
            # tier-derived), every escalation hop, and every same-tier retry
            # all pass through here before the effect is handed a credential.
            # This port never calls ``delta()``, so the OMN-17082 terminus
            # inside the routing authority does not cover it; the same guard
            # is applied here, on the CUSTOMER_LOCAL surface: a customer's
            # uncredentialed local rung is the honest terminus (nothing of
            # OmniNode's is involved on their own machine), a house-credentialed
            # backend is a typed refusal, and house/untenanted work is untouched.
            enforce_customer_key_terminus(
                tenant_id=resolved_tenant_id,
                task_type=task_type,
                correlation_id=correlation_id,
                surface=EnumDelegationSurface.CUSTOMER_LOCAL,
                api_key_ref=backend.secret_ref,
                api_key_env=backend.api_key_env,
                backend_ref=backend.backend_id,
                house_refs=shipped_house_credential_refs(),
            )

            # OMN-18297: input budget, checked BEFORE the call. A prompt above
            # this backend's contract-declared ``max_grounded_input_tokens`` is
            # not sent to it and is NOT truncated to fit -- truncating would
            # silently answer a different question, which is how a summary comes
            # back confidently citing pull requests the trimmed input never
            # contained. The ladder escalates instead; the receipt carries both
            # the budget and the measured input so the hop is attributable.
            measured_input_tokens = measure_grounding_input_tokens(prompt)
            grounding_budget = resolve_backend_grounding_budget(backend.backend_id)
            if (
                grounding_budget is not None
                and measured_input_tokens > grounding_budget
            ):
                current_tier = _routing_tier_name(backend)
                over_budget_message = (
                    f"input {measured_input_tokens} tokens exceeds backend "
                    f"{backend.backend_id}'s declared grounding budget of "
                    f"{grounding_budget} tokens; escalating rather than "
                    f"truncating. This is a GROUNDING budget, not the model's "
                    f"context window -- the prompt fits the window and would "
                    f"have been answered, ungrounded."
                )
                attempts.append(
                    {
                        "tier": current_tier,
                        "backend_id": backend.backend_id,
                        "model_id": backend.model_id,
                        "quality_gate_passed": False,
                        "quality_score": None,
                        "cost_usd": 0.0,
                        "failure_class": (
                            EnumDelegationFailureClass.CONTEXT_TOO_LARGE.value
                        ),
                        "error_message": over_budget_message,
                        "acceptance_decision": (
                            EnumDelegationAcceptanceDecision.CLIMB.value
                        ),
                        "acceptance_reason": (
                            EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED.value
                        ),
                        "input_tokens_measured": measured_input_tokens,
                        "input_token_budget": grounding_budget,
                    }
                )
                logger.info(
                    "LocalDelegationDispatch: over-grounding-budget task_type=%s "
                    "tier=%s backend=%s measured=%d budget=%d correlation=%s",
                    task_type,
                    current_tier,
                    backend.backend_id,
                    measured_input_tokens,
                    grounding_budget,
                    correlation_id,
                )
                excluded_tiers.add(current_tier)
                excluded_backend_refs.add(backend.backend_id)
                over_budget_next: ModelResolvedDelegationBackend | None = None
                if escalation_count < max_escalations:
                    over_budget_next = self._resolve_next_backend(
                        current_tier=current_tier,
                        task_type=task_type,
                        excluded_tiers=frozenset(excluded_tiers),
                        roi_overlay=roi_overlay,
                        excluded_backend_refs=frozenset(excluded_backend_refs),
                    )
                if over_budget_next is None:
                    # No rung can hold this input. Terminal FAILED naming the
                    # budget and the measurement -- never a silent truncation.
                    #
                    # OMN-18696 (second pass): deliberately does NOT attach a
                    # withheld-credential refusal, unlike the two terminals
                    # below. ``credential_withheld_rung`` probes selection with
                    # a 0-token estimate, so it cannot tell whether the rung it
                    # names would have held THIS input -- and a run that failed
                    # on a context ceiling must not be told its problem is a
                    # missing key. Widening it here means giving the query the
                    # real token estimate first, not adding the call.
                    return {
                        "status": "failed",
                        "content": best_content,
                        "delegated_to": backend.endpoint_ref,
                        "model_name": backend.model_id,
                        "quality_gate_passed": False,
                        "quality_score": 0.0,
                        "quality_gates_failed": [over_budget_message],
                        "delegation_latency_ms": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "correlation_id": str(correlation_id),
                        "escalation_count": escalation_count,
                        "cost_usd": float(cumulative_cost_usd),
                        "attempts": attempts,
                        "provenance": (
                            provenance.model_dump(mode="json")
                            if provenance is not None
                            else None
                        ),
                    }
                escalation_count += 1
                backend = over_budget_next
                continue
            attempt_outcome = await self._run_single_attempt(
                backend=backend,
                prompt=prompt,
                task_type=task_type,
                correlation_id=correlation_id,
                max_tokens=max_tokens,
                quality_contract_mode=quality_contract_mode,
                acceptance_criteria=acceptance_criteria,
                response_contract=response_contract,
                # OMN-15482: the caller's completion-shaping parameters apply to
                # EVERY attempt, including escalation hops -- a temperature or a
                # system role that silently reverted to the default after the
                # first retry would be the same fidelity defect, just harder to
                # observe.
                system_prompt=system_prompt,
                temperature=temperature,
                response_format=response_format,
            )

            # A hard transport/timeout failure: classify retryable vs terminal and,
            # when retryable, route through the SAME up-tier escalation the
            # quality-gate FAIL branch uses below (OMN-13943) — mirroring the bus
            # orchestrator's ``_should_escalate_inference_error`` posture (retry
            # unless PROVEN non-retryable). Only a non-retryable failure_class, or
            # an exhausted/unreachable escalation ladder, terminates FAILED without
            # trying a higher tier. This is what makes a GLM RATE_LIMITED (429)
            # transparently fall through to the next tier instead of terminating
            # the whole ``onex delegate`` call.
            transport_result: ModelLlmDelegationCallResult
            transport_failure_class: EnumDelegationFailureClass | None
            transport_failure_message: str
            transport_is_failure: bool
            if (
                attempt_outcome.failure_message is not None
                and attempt_outcome.result is None
            ):
                assert attempt_outcome.timeout_result is not None
                transport_result = attempt_outcome.timeout_result
                transport_failure_class = EnumDelegationFailureClass.TIMEOUT
                transport_failure_message = attempt_outcome.failure_message
                transport_is_failure = True
            else:
                assert attempt_outcome.result is not None
                transport_result = attempt_outcome.result
                if not transport_result.success:
                    transport_failure_class = transport_result.failure_class
                    transport_failure_message = (
                        transport_result.error_message or "delegation call failed"
                    )
                    transport_is_failure = True
                else:
                    transport_failure_class = None
                    transport_failure_message = ""
                    transport_is_failure = False

            if transport_is_failure:
                current_tier = _routing_tier_name(backend)
                excluded_backend_refs.add(backend.backend_id)

                # OMN-13640: exclude the BACKEND first, and only give up on the
                # TIER once the routing authority reports it has no untried
                # backend left for this task class. Excluding the tier on the
                # first backend failure — what this did before — made every
                # healthy sibling in it unreachable, which is how a Gemini
                # free-tier 429 terminated the whole delegation while the same
                # tier's GLM rung sat untried.
                transport_sibling: ModelResolvedDelegationBackend | None = None
                if _is_retryable_transport_failure(transport_failure_class):
                    transport_sibling = self._resolve_sibling_backend(
                        current_tier=current_tier,
                        task_type=task_type,
                        excluded_backend_refs=frozenset(excluded_backend_refs),
                    )
                if transport_sibling is None:
                    excluded_tiers.add(current_tier)

                escalated_backend: ModelResolvedDelegationBackend | None = None
                if (
                    transport_sibling is None
                    and _is_retryable_transport_failure(transport_failure_class)
                    and escalation_count < max_escalations
                ):
                    escalated_backend = self._resolve_next_backend(
                        current_tier=current_tier,
                        task_type=task_type,
                        excluded_tiers=frozenset(excluded_tiers),
                        roi_overlay=roi_overlay,
                        excluded_backend_refs=frozenset(excluded_backend_refs),
                    )

                # A transport failure never runs the quality gate, so bank its
                # (typically zero) metered cost directly — mirrors the
                # post-success banking below without requiring a gate verdict.
                cumulative_cost_usd += transport_result.actual_cost_usd
                cumulative_savings_usd += transport_result.savings_usd
                attempts.append(
                    {
                        "tier": current_tier,
                        "backend_id": backend.backend_id,
                        "model_id": backend.model_id,
                        "quality_gate_passed": False,
                        "quality_score": None,
                        "cost_usd": float(transport_result.actual_cost_usd),
                        "failure_class": (
                            transport_failure_class.value
                            if transport_failure_class is not None
                            else None
                        ),
                        # OMN-14063: surface WHY this tier was skipped (e.g. "endpoint
                        # ... failed health probe") on the attempt record itself, not
                        # only in the capture-file log line — a local->cloud escalation
                        # must be visible to the caller of ModelDelegateSkillResponse,
                        # not just an operator grepping logs after the fact.
                        "error_message": transport_failure_message,
                        # OMN-16932: the call never produced a response, so no
                        # accept/climb verdict was reachable. Typed as CLIMB /
                        # PROVIDER_CALL_FAILED rather than borrowed from the
                        # quality vocabulary, matching the bus path's record for
                        # the same situation.
                        "acceptance_decision": (
                            EnumDelegationAcceptanceDecision.CLIMB.value
                        ),
                        "acceptance_reason": (
                            EnumDelegationAcceptanceReason.PROVIDER_CALL_FAILED.value
                        ),
                    }
                )

                if transport_sibling is not None:
                    # A sideways hop inside the SAME tier. Not an escalation:
                    # ``escalation_count`` bounds how far UP the ladder a
                    # request may climb, and charging a sibling to it would let
                    # one tier's backend count exhaust the budget before the
                    # request ever reached a higher tier.
                    logger.info(
                        "LocalDelegationDispatch: same-tier sibling retry "
                        "task_type=%s tier=%s backend=%s -> backend=%s on "
                        "transport failure_class=%s correlation=%s reason=%s",
                        task_type,
                        current_tier,
                        backend.backend_id,
                        transport_sibling.backend_id,
                        transport_failure_class,
                        correlation_id,
                        transport_failure_message,
                    )
                    backend = transport_sibling
                    continue

                if escalated_backend is not None:
                    logger.info(
                        "LocalDelegationDispatch: escalating task_type=%s from "
                        "tier=%s to tier=%s on transport failure_class=%s "
                        "(attempt %d/%d) correlation=%s reason=%s",
                        task_type,
                        current_tier,
                        _routing_tier_name(escalated_backend),
                        transport_failure_class,
                        escalation_count + 1,
                        max_escalations,
                        correlation_id,
                        transport_failure_message,
                    )
                    escalation_count += 1
                    backend = escalated_backend
                    continue

                # Cannot escalate (non-retryable failure_class, budget exhausted,
                # or no higher eligible/resolvable tier): terminal FAILED, carrying
                # the cumulative metered cost of every attempt made so far.
                self._project_evidence(
                    correlation_id=correlation_id,
                    task_type=task_type,
                    endpoint_ref=backend.endpoint_ref,
                    model_id=backend.model_id,
                    result=transport_result,
                    prompt=prompt,
                    source_session_id=source_session_id,
                    tenant_id=resolved_tenant_id,
                    quality_passed=False,
                    failure_message=transport_failure_message,
                    cost_usd=cumulative_cost_usd,
                    savings_usd=cumulative_savings_usd,
                    escalation_count=escalation_count,
                    attempts=attempts,
                )
                return {
                    "status": "failed",
                    # OMN-14220: return the best authored artifact seen so far rather
                    # than nothing — a final transport failure (e.g. 429) must not
                    # discard a correct earlier-tier authorship.
                    "content": best_content,
                    # OMN-18696: carry the typed credential refusal, when the
                    # terminating attempt was one, so the CLI and the skill read
                    # the reference name and the remediation as fields. The key
                    # is absent (never a null placeholder) for every other
                    # terminal, so its presence is itself the refusal fact.
                    **(
                        {
                            "credential_refusal": (
                                transport_result.credential_refusal.model_dump(
                                    mode="json"
                                )
                            )
                        }
                        if transport_result.credential_refusal is not None
                        else {}
                    ),
                    # OMN-18696 (second pass): SEPARATE from the key above, and
                    # deliberately not folded into it. ``credential_refusal``
                    # means "this run was refused on a credential"; its mere
                    # presence is that fact, which
                    # ``test_a_non_credential_failure_carries_no_refusal_key``
                    # pins. This key means something weaker and different -- a
                    # rung the ladder never reached. Overloading one field with
                    # both would tell a consumer a run was refused on a
                    # credential when it failed on quality somewhere else.
                    **self._withheld_credential_rung(
                        task_type=task_type,
                    ),
                    "error_message": transport_failure_message,
                    "correlation_id": str(correlation_id),
                    "delegated_to": backend.endpoint_ref,
                    "model_name": backend.model_id,
                    "escalation_count": escalation_count,
                    "cost_usd": float(cumulative_cost_usd),
                    "attempts": attempts,
                    "provenance": (
                        provenance.model_dump(mode="json")
                        if provenance is not None
                        else None
                    ),
                }

            result = transport_result

            # This attempt's inference ran and incurred real metered cost — bank it
            # BEFORE deciding pass/fail so a rejected metered tier's spend is
            # counted even if we escalate away from it (OMN-13849).
            cumulative_cost_usd += result.actual_cost_usd
            cumulative_savings_usd += result.savings_usd

            attempt_tier = _routing_tier_name(backend)

            # OMN-14225: paid escalation is ON (metered) but NEVER SILENT. Any attempt
            # that incurred real metered spend is logged prominently — model,
            # task_type, tier, this attempt's cost, the running paid total for the
            # request, and the escalation depth (why we left the free tiers) — so a
            # paid GLM call can always be audited, meeting the original OMN-14097
            # "never silently spend" requirement without blocking the subscription-
            # covered paid tier.
            if result.actual_cost_usd > 0:
                logger.warning(
                    "PAID DELEGATION (metered): task_type=%s model=%s tier=%s "
                    "cost_usd=%.6f cumulative_paid_usd=%.6f escalation_count=%d "
                    "correlation=%s — escalated off the free tiers (local/frontier); "
                    "set ONEX_DELEGATION_ALLOW_PAID=0 to disable paid escalation.",
                    task_type,
                    backend.model_id,
                    attempt_tier,
                    float(result.actual_cost_usd),
                    float(cumulative_cost_usd),
                    escalation_count,
                    correlation_id,
                )

            gate_result = attempt_outcome.gate_result
            assert gate_result is not None
            preamble_chars = attempt_outcome.preamble_chars
            output_refusal = attempt_outcome.output_refusal
            quality_passed = self._is_quality_accepted(task_type, gate_result)
            # OMN-16932: the accept/climb verdict, typed, on the bus-less path
            # too — so `onex delegate` and the bus terminal describe a
            # stop-or-climb the same way instead of one of them leaving the
            # reader to infer it from a later rung appearing.
            # OMN-18379: and the REASON is now derived the way the bus path
            # derives it, three-way and naming the deciding blocking rule,
            # instead of labelling every refusal a bar miss.
            (
                acceptance_decision,
                acceptance_reason,
                acceptance_detail,
            ) = derive_attempt_acceptance(
                quality_passed=quality_passed,
                pre_filter_rejected=gate_result.fail_category == "fail_deterministic",
                gate_passed=gate_result.passed,
                judge_unavailable_floor=(
                    gate_result.passed
                    and gate_result.score_source
                    == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE
                ),
                quality_score=gate_result.quality_score,
                required_bar=_declared_required_bar(task_type),
                rule_evaluations=gate_result.rule_evaluations,
            )
            attempts.append(
                {
                    "tier": attempt_tier,
                    "backend_id": backend.backend_id,
                    "model_id": backend.model_id,
                    "quality_gate_passed": quality_passed,
                    "quality_score": gate_result.quality_score,
                    "cost_usd": float(result.actual_cost_usd),
                    "acceptance_decision": acceptance_decision.value,
                    "acceptance_reason": acceptance_reason.value,
                    "acceptance_detail": acceptance_detail,
                    # OMN-18379: what was removed from in front of the answer
                    # before any check ran, and which declared rule found the
                    # seam. Retained so a refusal can be audited against
                    # exactly the text that was judged.
                    "reasoning_preamble_rule": gate_result.reasoning_preamble_rule,
                    "reasoning_preamble": gate_result.reasoning_preamble,
                }
            )

            # OMN-14220: remember the best (highest-scoring) non-empty artifact so a
            # terminal failure can return real authored work instead of discarding it.
            #
            # OMN-18278: a response the provider cut off at its output-token
            # budget is NOT an artifact and is excluded here. It is not a
            # rejected-but-possibly-correct draft of the kind OMN-14220 exists
            # to preserve — the model never finished generating, so on the local
            # tier it is routinely the scratchpad alone. Without this the
            # exhaustion branch below hands that scratchpad back as ``content``
            # (``best_content`` starts at score -1.0, so a single truncated
            # attempt wins by default) and the caller's ``result.txt`` opens
            # with the model talking to itself. Skipping it costs nothing: a
            # later rung's real answer still fills ``best_content``, and when no
            # rung produces one the terminal is a failure carrying an empty
            # artifact, which is the truth.
            if (
                result.content
                and not is_truncated_by_output_budget(result.finish_reason)
                and gate_result.quality_score > best_content_score
            ):
                best_content = result.content
                best_content_score = gate_result.quality_score

            if quality_passed:
                # OMN-16419: prefer the LIVE-CONFIRMED served model id (set by
                # the fail-closed guard in HandlerLlmDelegationCall against a
                # real GET /v1/models read) over the configured backend.model_id
                # for attribution — the same OMN-8022-adjusted preference the
                # effect handler applies to the bus event. Falls back to
                # backend.model_id when the endpoint offered no /v1/models
                # evidence (e.g. a cloud backend), which is unchanged pre-ticket
                # behavior.
                attributed_model_id = result.served_model_id or backend.model_id
                # 5. PROJECTION — materialize the local evidence row from the
                #    terminal, carrying the REAL gate verdict and the CUMULATIVE
                #    metered cost across every attempt (never a hardcoded PASS,
                #    never a dropped rejected-attempt cost).
                self._project_evidence(
                    correlation_id=correlation_id,
                    task_type=task_type,
                    endpoint_ref=backend.endpoint_ref,
                    model_id=attributed_model_id,
                    result=result,
                    prompt=prompt,
                    source_session_id=source_session_id,
                    tenant_id=resolved_tenant_id,
                    quality_passed=True,
                    failure_message="",
                    cost_usd=cumulative_cost_usd,
                    savings_usd=cumulative_savings_usd,
                    escalation_count=escalation_count,
                    attempts=attempts,
                )
                return {
                    "status": "completed",
                    "content": result.content or "",
                    **(
                        {"output_refusal": output_refusal.model_dump(mode="json")}
                        if output_refusal is not None
                        else {}
                    ),
                    # The raw-to-deliverable boundary was applied before the gate.
                    # Preserve the measured removal on the terminal receipt while
                    # returning only the exact content the gate accepted.
                    "preamble_chars": preamble_chars,
                    # OMN-18695: carry the credential's provenance onto the
                    # terminal so the receipt records that the customer's own
                    # local store answered the reference. Read off the effect
                    # result, which observed the resolution; absent on the
                    # budget and transport-failure terminals above because no
                    # credential was resolved on those paths, and recording a
                    # source there would be a claim rather than an observation.
                    "secret_source": (
                        result.secret_source.value
                        if result.secret_source is not None
                        else None
                    ),
                    "secret_ref": result.secret_ref,
                    "delegated_to": backend.endpoint_ref,
                    "model_name": attributed_model_id,
                    "quality_gate_passed": True,
                    "quality_score": gate_result.quality_score,
                    "quality_gates_failed": list(gate_result.failure_reasons),
                    "delegation_latency_ms": result.latency_ms,
                    "input_tokens": result.tokens_in,
                    "output_tokens": result.tokens_out,
                    "total_tokens": result.tokens_in + result.tokens_out,
                    "correlation_id": str(correlation_id),
                    "escalation_count": escalation_count,
                    "cost_usd": float(cumulative_cost_usd),
                    "attempts": attempts,
                    "provenance": (
                        provenance.model_dump(mode="json")
                        if provenance is not None
                        else None
                    ),
                }

            # --- Quality-gate FAIL: evaluate escalation (mirror bus loop) -------
            gate_failure_message = "; ".join(gate_result.failure_reasons)
            current_tier = attempt_tier

            # OMN-14234 (retry-local / best-of-N): before escalating off a FREE
            # tier, retry the SAME backend up to its contract-declared max_retries
            # budget. The local coder is non-deterministic (~1/3 single-shot pass at
            # the 0.85 bar), so retrying a $0 draft lifts the local pass-rate before
            # crossing to a PAID tier. Mirrors the bus orchestrator's
            # ``_maybe_retry_local``: fail-closed — only a free tier with retry
            # budget remaining retries; a paid tier or an exhausted budget falls
            # through to the normal up-tier escalation. This retry does NOT touch
            # ``escalation_count`` or ``excluded_tiers`` (a same-tier retry is not a
            # tier escalation); the rejected draft's cost/tokens were already banked
            # and recorded above, and ``best_content`` already tracks it.
            if is_free_tier(current_tier) and local_retry_counts.get(
                current_tier, 0
            ) < tier_max_retries(current_tier):
                local_retry_counts[current_tier] = (
                    local_retry_counts.get(current_tier, 0) + 1
                )
                logger.info(
                    "LocalDelegationDispatch: retry-local task_type=%s tier=%s "
                    "draft %d/%d ($0 re-draft before escalating off free tier) "
                    "correlation=%s reason=%s",
                    task_type,
                    current_tier,
                    local_retry_counts[current_tier],
                    tier_max_retries(current_tier),
                    correlation_id,
                    gate_failure_message,
                )
                continue

            excluded_backend_refs.add(backend.backend_id)

            # OMN-13640: same posture as the transport branch above — the tier
            # is only abandoned once the routing authority reports no untried
            # backend left in it for this task class. A quality rejection is a
            # verdict on THIS backend's draft, never on the tier's other
            # backends, which have not been asked yet.
            gate_sibling = self._resolve_sibling_backend(
                current_tier=current_tier,
                task_type=task_type,
                excluded_backend_refs=frozenset(excluded_backend_refs),
            )
            if gate_sibling is None:
                excluded_tiers.add(current_tier)

            # OMN-14004: persist the rejected candidate's own content, not just the
            # failure reason. Before this the capture log (and the terminal
            # payload's cumulative ``content``) only ever carried the LAST
            # attempt's text — an earlier tier's rejected-but-potentially-correct
            # answer (e.g. a false-reject) was unrecoverable once escalation
            # overwrote ``result``. The capture-file this logger writes to is
            # already promoted to a content-addressed artifact by the CLI receipt
            # layer (``receipt_mode.py``), so logging the full candidate here is
            # enough to make it durable evidence without a new persistence surface.
            logger.info(
                "LocalDelegationDispatch: rejected candidate content "
                "(task_type=%s tier=%s correlation=%s reason=%s):\n%s",
                task_type,
                current_tier,
                correlation_id,
                gate_failure_message,
                result.content or "",
            )

            if gate_sibling is not None:
                logger.info(
                    "LocalDelegationDispatch: same-tier sibling retry "
                    "task_type=%s tier=%s backend=%s -> backend=%s after a "
                    "quality-gate rejection correlation=%s reason=%s",
                    task_type,
                    current_tier,
                    backend.backend_id,
                    gate_sibling.backend_id,
                    correlation_id,
                    gate_failure_message,
                )
                backend = gate_sibling
                continue

            next_backend: ModelResolvedDelegationBackend | None = None
            if escalation_count < max_escalations:
                next_backend = self._resolve_next_backend(
                    current_tier=current_tier,
                    task_type=task_type,
                    excluded_tiers=frozenset(excluded_tiers),
                    roi_overlay=roi_overlay,
                    excluded_backend_refs=frozenset(excluded_backend_refs),
                )

            if next_backend is None:
                # Cannot escalate (budget exhausted or no higher eligible tier):
                # terminal FAILED, carrying the cumulative metered cost of every
                # attempt made so far.
                self._project_evidence(
                    correlation_id=correlation_id,
                    task_type=task_type,
                    endpoint_ref=backend.endpoint_ref,
                    model_id=backend.model_id,
                    result=result,
                    prompt=prompt,
                    source_session_id=source_session_id,
                    tenant_id=resolved_tenant_id,
                    quality_passed=False,
                    failure_message=gate_failure_message,
                    cost_usd=cumulative_cost_usd,
                    savings_usd=cumulative_savings_usd,
                    escalation_count=escalation_count,
                    attempts=attempts,
                )
                return {
                    "status": "failed",
                    # OMN-14220: prefer the best authored artifact across all attempts
                    # (highest gate score, non-empty) over the LAST attempt's content —
                    # a later tier that scored lower (or returned empty) must not
                    # overwrite a correct earlier authorship the gate (falsely) rejected.
                    #
                    # OMN-18278: and when the LAST attempt was cut off at its
                    # output-token budget, this fallback yields nothing rather
                    # than that fragment. Guarding only ``best_content`` above
                    # would have been cosmetic: with a single truncated attempt
                    # ``best_content`` is empty and control reaches exactly this
                    # ``or``, which is how the scratchpad was surfaced as the
                    # answer in the first place.
                    "content": _terminal_artifact(best_content, result),
                    **(
                        {"output_refusal": output_refusal.model_dump(mode="json")}
                        if output_refusal is not None
                        else {}
                    ),
                    "preamble_chars": preamble_chars,
                    # OMN-18696 (second pass): a quality terminal is the case
                    # the absent-key measurement actually produced -- the ladder
                    # exhausted the rungs it COULD route and failed on quality,
                    # while the rung that needed a key was never tried. Carried
                    # under its own key, never under ``credential_refusal``:
                    # this run was NOT refused on a credential, and saying so
                    # would be false.
                    **self._withheld_credential_rung(
                        task_type=task_type,
                    ),
                    # OMN-18695: carry the credential's provenance onto the
                    # terminal so the receipt records that the customer's own
                    # local store answered the reference. Read off the effect
                    # result, which observed the resolution; absent on the
                    # budget and transport-failure terminals above because no
                    # credential was resolved on those paths, and recording a
                    # source there would be a claim rather than an observation.
                    "secret_source": (
                        result.secret_source.value
                        if result.secret_source is not None
                        else None
                    ),
                    "secret_ref": result.secret_ref,
                    "delegated_to": backend.endpoint_ref,
                    "model_name": backend.model_id,
                    "quality_gate_passed": False,
                    "quality_score": gate_result.quality_score,
                    "quality_gates_failed": list(gate_result.failure_reasons),
                    "delegation_latency_ms": result.latency_ms,
                    "input_tokens": result.tokens_in,
                    "output_tokens": result.tokens_out,
                    "total_tokens": result.tokens_in + result.tokens_out,
                    "correlation_id": str(correlation_id),
                    "escalation_count": escalation_count,
                    "cost_usd": float(cumulative_cost_usd),
                    "attempts": attempts,
                    "provenance": (
                        provenance.model_dump(mode="json")
                        if provenance is not None
                        else None
                    ),
                }

            # Escalate: advance to the next tier's backend and retry.
            logger.info(
                "LocalDelegationDispatch: escalating task_type=%s from tier=%s to "
                "tier=%s (attempt %d/%d) correlation=%s reason=%s",
                task_type,
                current_tier,
                _routing_tier_name(next_backend),
                escalation_count + 1,
                max_escalations,
                correlation_id,
                gate_failure_message,
            )
            escalation_count += 1
            backend = next_backend

    def _is_quality_accepted(
        self, task_type: str, gate_result: ModelQualityGateResult
    ) -> bool:
        """Apply the task-class required-bar authority, mirroring the bus path.

        The quality-gate reducer's ``passed`` alone is NOT the acceptance verdict
        for a verifiable task class: the reducer returns ``passed=True`` for a code
        answer that clears the deterministic FLOOR even at a graded score below the
        class ``required_bar`` (e.g. code_generation ~0.733 < 0.85). The bus
        orchestrator (``handle_gate_result`` :1240-1253) accepts only when the gate
        passed AND the score is at/above the contract ``required_bar`` AND the
        result is not a deterministic-floor rejection. This method replicates that
        rule so the local path applies the same 0.85 bar the bus applies.

        When the task class declares no ``quality_gate.required_bar`` (the legacy /
        no-contract-DoD path), no bar can be applied and the reducer verdict
        ``passed`` is the authority — preserving the pre-OMN-13849 behavior for
        classes without a declared bar.

        OMN-13959 — judge-unavailable degraded acceptance. For a VERIFIABLE task
        class the reducer records ``score_source=deterministic_acceptance`` (rather
        than ``combined``) ONLY when the deterministic acceptance FLOOR passed but
        the LLM-judge adequacy score was NOT combined — i.e. the judge call failed
        / was unreachable (``JUDGE_FAILED``: e.g. the cloud judge is 429-throttled).
        In that state the combined-score ``required_bar`` (0.85) is structurally
        un-meetable, because the judge's semantic-adequacy band (weight 0.4) is
        absent and the deterministic-only graded score tops out below the bar
        (~0.733). Applying the combined bar would reject a valid LOCAL artifact that
        cleared the real DoD floor and escalate it to ladder exhaustion during a
        cloud-judge outage — defeating local-first. Fall back to the deterministic
        FLOOR verdict (the real DoD checks: compiles / final-artifact-only /
        non-refusal / non-empty) instead of a bar the judge band is required to
        reach. This does NOT weaken the bar: when the judge IS reachable the score
        is combined (``score_source=combined``) and the full bar still applies; a
        deterministic-floor REJECTION returns ``fail_deterministic`` and is refused
        above; a judge FAIL veto returns ``passed=False`` and is refused below.
        """
        if gate_result.fail_category == "fail_deterministic":
            return False
        if (
            gate_result.passed
            and gate_result.score_source == SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE
        ):
            return True
        try:
            authority = resolve_required_bar_authority(task_type=task_type)
        except RequiredBarAuthorityError:
            # No declared bar for this class — the reducer verdict is authoritative.
            return gate_result.passed
        if gate_result.quality_score < authority.required_bar:
            return False
        return gate_result.passed

    def _withheld_credential_rung(
        self,
        *,
        task_type: str,
    ) -> dict[str, object]:
        """``{"credential_withheld": ...}`` for a rung the LADDER never reached.

        OMN-18696 gave the local path a typed refusal for a credential the
        provider REJECTED, at the effect boundary. An ABSENT credential never
        reaches that boundary on an ordinary run: a backend whose declared
        reference resolves to nothing is not routable, so the ladder does not
        select it, so nothing refuses and nothing is said. Measured on this Mac
        2026-09-19, the same command twice with only the store changed -- with a
        (bad) value registered the run reached ``cheap_frontier`` and returned
        ``credential_rejected`` naming the reference; with the reference
        deleted it made three local attempts, ``escalation_count`` 0, and
        carried no credential field at all. The customer who had registered
        nothing was told less than the one who had registered something wrong.

        This closes that asymmetry WITHOUT changing which backend is selected.
        The rung stays withheld and the ladder still degrades to whatever it can
        serve -- a machine holding no cloud credentials must keep working on its
        local tier, which is the whole premise of the local path. What changes
        is that a FAILED terminal now names the rung it did not get to try and
        the reference that would have unlocked it.

        WHY THIS IS NOT ``credential_refusal``, which already exists and would
        have been fewer lines: that key means "this run was REFUSED on a
        credential", and its mere presence is that fact --
        ``test_a_non_credential_failure_carries_no_refusal_key`` pins exactly
        that, and an earlier revision of this change broke it. A withheld rung
        is a weaker and different claim: the run failed for its own reasons and
        a cheaper rung was never available. Folding the two together would tell
        a consumer that a run which failed on quality at the local tier had
        been refused on a credential. Two facts, two fields.

        Returns ``{}`` when nothing was withheld, so the key stays absent rather
        than null on every other terminal.
        """
        rung = credential_withheld_rung(task_type)
        if rung is None:
            return {}
        logger.warning(
            "delegation_credential_withheld reason=%s class=%s tier=%s "
            "backend=%s credential=%s",
            EnumLocalCredentialRefusalReason.CREDENTIAL_ABSENT.value,
            EnumDelegationFailureClass.PROVIDER_CREDENTIAL_MISSING.value,
            rung.tier,
            rung.endpoint_url,
            rung.credential_ref,
        )
        return {"credential_withheld": rung.model_dump(mode="json")}

    def _resolve_initial_backend(
        self,
        task_type: str,
        *,
        roi_overlay: ModelRoutingRoiOverlay | None = None,
        backend_id: str | None = None,
    ) -> ModelResolvedDelegationBackend:
        """Resolve the cheapest-first INITIAL backend via the task-class tier_order.

        OMN-15156: when ``backend_id`` is supplied it is a caller-supplied PIN —
        it bypasses cheapest-first ``tier_order`` selection entirely and resolves
        the EXACT backend requested via ``resolve_delegation_backend``'s existing
        ``backend_id`` kwarg, the SAME targeted-resolution path the LLM-judge
        already uses to pin a concrete backend (OMN-13470). An unknown/
        unresolvable ``backend_id`` is NOT caught here: ``resolve_delegation_backend``
        raises ``RuntimeError`` (see its docstring) and it propagates verbatim —
        fail loudly, never a silent fallback to the untargeted/tier-based
        resolution. This is deliberately asymmetric with the tier-based branch
        below (which DOES catch ``RuntimeError`` and falls back): a resolution
        failure on the closed-set ladder's own chosen tier means "try the
        untargeted path instead," but a resolution failure on a caller's EXPLICIT
        pin means the caller asked for a backend that doesn't exist, and hiding
        that behind a silent fallback would defeat the whole point of pinning.

        Escalation-interaction note (OMN-15156, P0 readback requirement): this pin
        affects ONLY the initial attempt. If the pinned backend's transport call
        fails, the existing escalation loop in ``dispatch()`` still excludes the
        pinned backend's WHOLE TIER (via ``tier_for_backend(backend.backend_id)``)
        and re-resolves the next hop through the normal closed-set ``tier_order``
        — NOT back to the pin, and NOT to a different backend within the same
        tier. This ticket does not redesign escalation; a single-backend (rather
        than whole-tier) exclusion semantics is out of scope here.

        OMN-14001: when ``roi_overlay`` demotes a proven-failing tier,
        ``first_eligible_tier`` returns the cheapest tier NOT ROI-suppressed, so a
        stored outcome changes which backend the initial resolution lands on. The
        overlay's own fail-safe keeps a fully-suppressed ladder resolvable.

        OMN-13861: the initial resolution MUST consult the closed-set task-class
        ``escalation_policy.tier_order`` — exactly like every escalation hop already
        does (``_resolve_next_backend``) — instead of the untargeted
        ``resolve_delegation_backend(task_type)``. The untargeted call selected the
        first bifrost-file-order backend whose ``endpoint_url`` was populated and
        whose capabilities matched the task, which for ``code_generation`` was the
        abandoned off-ladder ``cloud-gemini-pro`` (OMN-13667). That VIOLATED both
        binding guardrails (cheapest-first + closed-set tier_order) and, because
        ``tier_for_backend`` cannot classify an off-ladder backend,
        ``next_eligible_tier`` returned None immediately — stranding the OMN-13849
        escalation loop after a single attempt.

        The first eligible tier is resolved through the routing authority
        (``first_eligible_tier`` → ``backend_id_for_tier`` →
        ``resolve_delegation_backend(task_type, backend_id=...)``), so the initial
        backend is the cheapest tier the closed-set ladder actually declares. Falls
        back to the untargeted resolution only when the task class declares no
        routable tier_order (legacy / no-contract classes), preserving their
        behavior without opening the closed set for classes that DO declare one.
        """
        if backend_id is not None:
            return resolve_delegation_backend(task_type, backend_id=backend_id)
        first_tier = first_eligible_tier(task_type, roi_overlay=roi_overlay)
        if first_tier is not None:
            backend_id = backend_id_for_tier(first_tier, task_type)
            if backend_id is not None:
                try:
                    return resolve_delegation_backend(task_type, backend_id=backend_id)
                except RuntimeError:
                    # The first tier's backend has no populated COMPLETE endpoint in
                    # the active overlay — fall back to the untargeted resolution
                    # rather than fail the dispatch outright.
                    logger.warning(
                        "LocalDelegationDispatch: initial tier=%s backend=%s has no "
                        "resolvable endpoint for task_type=%s; falling back to "
                        "untargeted resolution",
                        first_tier,
                        backend_id,
                        task_type,
                    )
        return resolve_delegation_backend(task_type)

    def _resolve_sibling_backend(
        self,
        *,
        current_tier: str,
        task_type: str,
        excluded_backend_refs: frozenset[str],
    ) -> ModelResolvedDelegationBackend | None:
        """Resolve an untried sibling backend inside ``current_tier`` (OMN-13640).

        OMN-14402 added a same-tier backend fallback so that ONE backend's
        transport failure does not walk the request off the tier while the tier
        still declares a healthy backend for the task class. That fallback was
        only ever wired into the BUS orchestrator
        (``handler_delegation_workflow._maybe_retry_sibling_backend``). This
        bus-less local path — the one ``onex delegate`` runs — never had it, and
        instead added the WHOLE tier to ``excluded_tiers`` on the first backend
        failure, which makes every sibling in it unreachable for the rest of the
        dispatch.

        Measured consequence, 2026-09-15, three consecutive runs of a 194-word
        prose prompt in task class ``research``: ``cloud-gemini-pro`` returned a
        free-tier-quota 429 (``failure_class=rate_limited``, retryable), the
        port excluded ``cheap_cloud`` whole, ``next_eligible_tier`` found no
        higher tier offering a non-excluded backend, and the delegation
        terminated FAILED — while ``sibling_backend_available_in_tier(
        "cheap_cloud", "research", frozenset({"cloud-gemini-pro"}))`` reported
        ``cloud-glm``, an untried, separately-funded, eligible sibling.

        Eligibility is decided by the routing authority, never here:
        ``sibling_backend_available_in_tier`` runs the SAME deterministic
        ``_select_model_for_task`` selection the initial route ran, in
        ``routing_tiers.yaml`` declaration order. This method only resolves the
        reported ``backend_ref`` into a COMPLETE-endpoint backend, and treats an
        unresolvable endpoint the way ``_resolve_next_backend`` does — as one
        more excluded candidate rather than a hard stop, so a tier whose second
        backend is unbindable still reaches its third.

        Bounded by construction: every candidate it returns is added to
        ``excluded_backend_refs`` by the caller before the next call, so the
        search walks each backend the tier declares for ``task_type`` at most
        once and then returns ``None``. A sideways hop is NOT a tier escalation
        and the caller must not charge it to ``escalation_count`` — the same
        posture the bus path takes by returning before ``_decide_escalation``.
        """
        tried: set[str] = set(excluded_backend_refs)
        while True:
            sibling_ref = sibling_backend_available_in_tier(
                current_tier,
                task_type,
                frozenset(tried),
            )
            if sibling_ref is None:
                return None
            tried.add(sibling_ref)
            try:
                return resolve_delegation_backend(task_type, backend_id=sibling_ref)
            except RuntimeError:
                # No populated COMPLETE endpoint in the active overlay. Skip it
                # and ask the authority for the next declared sibling rather
                # than abandoning the tier on one unbindable entry.
                logger.warning(
                    "LocalDelegationDispatch: same-tier sibling tier=%s "
                    "backend=%s has no resolvable endpoint for task_type=%s; "
                    "trying the next sibling the tier declares",
                    current_tier,
                    sibling_ref,
                    task_type,
                )

    def _resolve_next_backend(
        self,
        *,
        current_tier: str,
        task_type: str,
        excluded_tiers: frozenset[str],
        roi_overlay: ModelRoutingRoiOverlay | None = None,
        excluded_backend_refs: frozenset[str] = frozenset(),
    ) -> ModelResolvedDelegationBackend | None:
        """Resolve the next eligible tier's backend, or None if none exists.

        OMN-14001: ``roi_overlay`` (when set) demotes ROI-suppressed tiers on the
        escalation hop too, with the overlay's fail-safe second pass keeping the
        ladder reachable when suppression would exhaust it.

        OMN-15803: ``excluded_backend_refs`` carries every ``backend_id`` already
        attempted anywhere earlier in THIS dispatch (mirrors the bus
        orchestrator / OMN-15503's ``next_eligible_tier(...,
        excluded_backend_refs=...)`` parameter, which this local bus-less path
        previously never threaded). Two DIFFERENT routing tiers can declare the
        SAME concrete bifrost backend for a task type (e.g. ``cheap_cloud`` and
        ``claude`` both resolve ``research`` to ``cloud-gemini-pro`` in the live
        contract) — without this, escalating "up" a tier that only offers an
        already-failed backend re-dispatches the IDENTICAL backend+model, a
        functional no-op. ``next_eligible_tier`` skips any tier whose only
        routable backend is excluded; the defensive ``backend_id in
        excluded_backend_refs`` check below additionally guards against
        ``backend_id_for_tier`` (which does not itself accept an exclusion set)
        re-selecting an excluded backend for a multi-backend tier that
        ``next_eligible_tier`` judged eligible via a DIFFERENT candidate.

        Mirrors the bus orchestrator's ``_decide_escalation`` tier resolution
        (:748-811): ``next_eligible_tier`` reads the closed-set task-class
        ``tier_order`` (never appends an unlisted tier — OMN-13140) and skips tiers
        that cannot route the task; ``backend_id_for_tier`` then maps the resolved
        tier to the concrete bifrost backend the tier would select, which is
        re-resolved into a COMPLETE-endpoint ``ModelResolvedDelegationBackend``.
        Returns None when the ladder is exhausted or the escalated backend has no
        populated endpoint in the local overlay (fail-closed, no silent hang).
        """
        next_tier = next_eligible_tier(
            current_tier,
            excluded_tiers,
            task_type=task_type,
            roi_overlay=roi_overlay,
            excluded_backend_refs=excluded_backend_refs,
        )
        if next_tier is None:
            return None
        backend_id = backend_id_for_tier(next_tier, task_type)
        if backend_id is None:
            return None
        if backend_id in excluded_backend_refs:
            # Defensive backstop (OMN-15803): next_eligible_tier judged this
            # tier eligible (a non-excluded candidate exists somewhere in it),
            # but backend_id_for_tier's own selection -- which does not accept
            # an exclusion set -- re-picked the SAME excluded backend by its
            # normal file-order/use_for priority. Treat as unresolvable rather
            # than dispatch an identical (backend_id, model_id) pair twice.
            logger.warning(
                "LocalDelegationDispatch: escalation tier=%s re-selected "
                "already-excluded backend=%s for task_type=%s; ladder exhausted",
                next_tier,
                backend_id,
                task_type,
            )
            return None
        try:
            return resolve_delegation_backend(task_type, backend_id=backend_id)
        except RuntimeError:
            # The escalated tier's backend has no populated COMPLETE endpoint in
            # the active overlay — treat the ladder as exhausted rather than
            # dispatching to an unresolvable endpoint.
            logger.warning(
                "LocalDelegationDispatch: escalation tier=%s backend=%s has no "
                "resolvable endpoint for task_type=%s; ladder exhausted",
                next_tier,
                backend_id,
                task_type,
            )
            return None

    async def _run_single_attempt(
        self,
        *,
        backend: ModelResolvedDelegationBackend,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int | None,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        response_contract: dict[str, object] | None = None,
        system_prompt: str | None = None,
        temperature: float | None = None,
        response_format: dict[str, object] | None = None,
    ) -> _AttemptOutcome:
        """Run one resolve->effect->gate attempt for ``backend``.

        Returns the effect result + gate verdict on a completed call, or a
        canonical TIMEOUT/transport failure marker the caller projects. This is
        exactly the single-shot behavior the port had before OMN-13849; the
        escalation loop calls it once per tier.

        OMN-15482: ``system_prompt`` / ``temperature`` / ``response_format`` are
        the caller's completion-shaping parameters. ``None`` on each reproduces
        the pre-existing behavior exactly -- the task-type default system
        prompt, ``ModelLlmDelegationCallRequest``'s own default temperature, and
        no ``response_format`` key on the outbound payload.
        """
        # 2. RESOLVE the effective output-token budget from the routing contract.
        #    Unset request -> backend ceiling; explicit request -> capped at it.
        effective_max_tokens = resolve_effective_max_tokens(
            requested=max_tokens, backend_max_tokens=backend.max_tokens
        )

        # 2b. RESOLVE the per-call HTTP timeout from the routing contract (÷1000),
        #     so the transport honors the backend's configured timeout_ms instead
        #     of a hardcoded cap (OMN-13170).
        timeout_seconds = resolve_timeout_seconds(backend_timeout_ms=backend.timeout_ms)

        # Inference-protocol shaping (e.g. /no_think prefix, chat_template_kwargs).
        #
        # OMN-15482: a caller-supplied ``system_prompt`` REPLACES the task-type
        # default rather than being appended to the user prompt. This is what
        # makes the system/user role split survive the delegation path: the
        # effect handler emits ``system_prompt`` as a distinct ``role: system``
        # chat message, so a client that previously sent two roles over direct
        # HTTP still sends two roles here. ``None`` keeps the pre-existing
        # task-type default verbatim. Inference-protocol shaping still applies
        # on top either way -- a caller's system prompt is a system prompt, not
        # an escape from backend-specific directives such as ``/no_think``.
        base_system_prompt = (
            system_prompt
            if system_prompt is not None
            else _TASK_TYPE_SYSTEM_PROMPTS.get(
                task_type, _TASK_TYPE_SYSTEM_PROMPTS["research"]
            )
        )
        # OMN-7942: resolve the EFFECTIVE response contract ONCE, here, before
        # the call -- then use the same value to instruct the model and to
        # grade its answer.
        #
        # The caller's declared contract used to reach only the quality gate.
        # Measured on the live .201 lab endpoint 2026-09-18, correlation
        # 4c053fe9-1fce-4204-8705-2ed009fdc32d, the served model's own recorded
        # reasoning read "We have no explicit schema.", it guessed a key name
        # the contract does not contain, three local attempts failed the
        # deterministic floor, and the router climbed to cheap_cloud and spent
        # $0.003856. Two other models on two other providers failed the same
        # gate in the same run, which is what rules out a model-quality
        # reading.
        #
        # Resolving here rather than inside ``_evaluate_quality_gate`` is the
        # load-bearing half. The task-class DEFAULT contract (OMN-15196) was
        # resolved inside the gate -- that is, AFTER the model had answered --
        # so a class-defaulted schema could not have been shown to the model
        # even in principle. One resolution threaded to both surfaces makes
        # "instructed" and "graded" the same value by construction rather than
        # by two call sites happening to agree.
        effective_response_contract = response_contract
        if effective_response_contract is None:
            effective_response_contract = resolve_task_class_response_contract(
                task_type
            )
        deliverable_contract = resolve_task_class_deliverable_contract(
            task_type,
            effective_response_contract,
        )
        resolved_system_prompt = compose_system_prompt_with_response_contract(
            system_prompt=base_system_prompt,
            response_contract=effective_response_contract,
            output_shape=deliverable_contract.output_shape.value,
            render_start_marker=deliverable_contract.render_start_marker,
        )
        (
            outbound_system_prompt,
            outbound_prompt,
            provider_request_options,
        ) = apply_inference_protocol(
            system_prompt=resolved_system_prompt,
            prompt=prompt,
            model=backend.model_id,
            task_type=task_type,
            backend_id=backend.backend_id,
        )
        reserved = _RESERVED_PROVIDER_REQUEST_KEYS.intersection(
            provider_request_options
        )
        if reserved:
            keys = ", ".join(sorted(reserved))
            raise ValueError(f"provider request options cannot override: {keys}")

        logger.info(
            "LocalDelegationDispatch: task_type=%s backend=%s model=%s correlation=%s",
            task_type,
            backend.backend_id,
            backend.model_id,
            correlation_id,
        )

        # 3. CANONICAL EFFECT HANDLER — one LLM call, transport selected by profile.
        call_request = ModelLlmDelegationCallRequest(
            request_id=str(uuid.uuid4()),
            correlation_id=str(correlation_id),
            causation_id=str(correlation_id),
            model_id=backend.model_id,
            # OMN-18670: carry WHICH artifact supplied model_id, so the
            # fail-closed attribution refusal at the effect boundary names the
            # overlay file and key rather than only the stale literal.
            model_id_source=backend.model_id_source,
            endpoint_ref=backend.endpoint_ref,
            prompt=outbound_prompt,
            prompt_hash="",
            system_prompt=outbound_system_prompt,
            task_type=task_type,
            max_tokens=effective_max_tokens,
            timeout_seconds=timeout_seconds,
            # OMN-15803: the ROUTING-authority tier name (local/cheap_cloud/
            # claude), never the raw bifrost-declared ``.tier`` label — the
            # effect boundary's pricing lookup
            # (handler_llm_delegation_call._get_tier_price_per_1m /
            # _FALLBACK_PRICE_PER_1M) keys on routing_tiers.yaml vocabulary, so
            # a bifrost label here (e.g. "frontier_api") silently mispriced
            # every ceiling-tier call to the generic fallback rate.
            model_tier=_routing_tier_name(backend),
            provider=backend.backend_id,
            extra_headers=backend.extra_headers,
            provider_request_options=provider_request_options,
            # OMN-13861: carry the backend's logical secret reference so the effect
            # handler can resolve it to an Authorization header at the call boundary.
            # An authenticated cloud tier now attaches credentials on the bus-less
            # local path; an unauthenticated local backend carries None.
            secret_ref=backend.secret_ref,
            # OMN-13943: carry the backend's own contract-declared literal env-var
            # name as an ADDITIONAL fallback the effect resolves when the
            # secret_ref convention mapping misses (e.g. GEMINI_API_KEY /
            # OPEN_ROUTER_API_KEY drift against the LLM_*_API_KEY convention).
            api_key_env=backend.api_key_env,
            # OMN-15482: the caller's response-format directive, forwarded as a
            # real wire parameter on the outbound chat-completions payload.
            # ``None`` omits the key entirely (pre-existing behavior).
            response_format=response_format,
            # OMN-15482: the caller's sampling temperature, falling back to the
            # effect model's OWN declared default (read off the model, not
            # re-typed here, so the two can never drift apart). This
            # byte-preserves the pre-existing outbound payload for every caller
            # that does not set a temperature.
            temperature=(
                temperature if temperature is not None else _DEFAULT_CALL_TEMPERATURE
            ),
        )
        # OMN-13597: the effect handler is a synchronous blocking call (health
        # probe + curl/httpx LLM POST). Awaiting it inline blocks the asyncio
        # event loop the local runtime drives — the in-memory bus delivers the
        # command synchronously inside ``bus.publish`` (``await callback(...)``),
        # so the handler runs to completion *before* ``bus.publish`` returns and
        # ``RuntimeLocal`` never reaches its terminal-wait timeout. On an
        # unreachable endpoint (e.g. the local model host not routable from the
        # CLI's container) a connect that stalls below the OS level defeats the
        # transport's own ``--max-time``/httpx bound and the whole ``onex
        # delegate`` CLI hangs forever — no output, no evidence row.
        #
        # Fix: run the blocking sync effect behind a supervised child-process
        # boundary and poll it from the loop. The process boundary is intentionally
        # stronger than ``asyncio.to_thread``: when the hard deadline expires, the
        # worker can be terminated so ``asyncio.run`` has no orphaned thread to join.
        dispatch_deadline_seconds = timeout_seconds + _DISPATCH_TIMEOUT_BUFFER_SECONDS
        try:
            if self._effect_process_boundary:
                result = await _run_effect_handler_with_killable_timeout(
                    self._effect_handler,
                    call_request,
                    timeout_seconds=dispatch_deadline_seconds,
                )
            else:
                result = self._effect_handler(call_request)
        except TimeoutError:
            failure_message = (
                f"delegation call did not return within "
                f"{dispatch_deadline_seconds:.0f}s (endpoint {backend.endpoint_ref} "
                f"unreachable or unresponsive)"
            )
            logger.warning(
                "LocalDelegationDispatch: %s correlation=%s",
                failure_message,
                correlation_id,
            )
            # Build a canonical TIMEOUT failure result (public model surface) so
            # the evidence row is materialized through the SAME projection path as
            # a transport failure — never PASS, never silent.
            timeout_result = ModelLlmDelegationCallResult(
                request_id=call_request.request_id,
                success=False,
                failure_class=EnumDelegationFailureClass.TIMEOUT,
                error_message=failure_message,
                endpoint_healthy=False,
            )
            return _AttemptOutcome(
                result=None,
                gate_result=None,
                failure_message=failure_message,
                timeout_result=timeout_result,
                preamble_chars=0,
                output_refusal=None,
            )

        if not result.success:
            return _AttemptOutcome(
                result=result,
                gate_result=None,
                failure_message=None,
                timeout_result=None,
                preamble_chars=0,
                output_refusal=None,
            )

        extraction = extract_deliverable(result.content or "", deliverable_contract)
        output_refusal: ModelDelegationOutputRefusal | None = None
        if extraction.refusal in {
            EnumDeliverableExtractionRefusal.AMBIGUOUS_UNMARKED,
            EnumDeliverableExtractionRefusal.NO_SCHEMA_CONFORMING_JSON,
        }:
            output_refusal = ModelDelegationOutputRefusal(
                reason=EnumDelegationOutputRefusalReason(extraction.refusal.value),
                output_shape=deliverable_contract.output_shape,
                contract_failure_reasons=extraction.contract_failure_reasons,
            )
            result = result.model_copy(update={"content": ""})
        else:
            result = result.model_copy(update={"content": extraction.deliverable})

        # 4. CANONICAL QUALITY GATE (OMN-13597) — run the SAME reducer the bus
        #    path runs. HTTP/transport success is NOT a quality verdict: a model
        #    refusal or empty answer returns success here but must NOT be recorded
        #    as a gate PASS. Resolve the task-class DoD checks from the routing
        #    authority and evaluate the real verdict + graded score, threading the
        #    LLM-judge adequacy score for combinable task classes (OMN-13849).
        gate_result = await self._evaluate_quality_gate(
            correlation_id=correlation_id,
            task_type=task_type,
            prompt=prompt,
            content=result.content or "",
            quality_contract_mode=quality_contract_mode,
            acceptance_criteria=acceptance_criteria,
            # OMN-7942: the ALREADY-RESOLVED contract, not the caller's raw
            # one. The gate resolves a task-class default of its own when
            # handed None; passing the resolved value makes that a no-op and
            # removes the second, independent resolution that could otherwise
            # grade against a schema the model was not shown.
            response_contract=effective_response_contract,
            deliverable_evidence=ModelDelegationDeliverableEvidence(
                output_shape=deliverable_contract.output_shape,
                contract_sha256=canonical_deliverable_contract_sha256(
                    deliverable_contract
                ),
                deliverable_sha256=hashlib.sha256(
                    (result.content or "").encode()
                ).hexdigest(),
                deliverable_chars=len(result.content or ""),
                preamble_chars=extraction.preamble_chars,
                raw_chars=extraction.raw_chars,
                deliverable_start=extraction.deliverable_start,
                deliverable_end=extraction.deliverable_end,
            ),
            # OMN-18278: what the PROVIDER said about this response, not what
            # the text says about itself. A response cut off by the output-token
            # budget stopped mid-thought, so the model never emitted the
            # terminator OMN-18379's segmenter cuts at and the scratchpad is the
            # whole response — which every content heuristic reads as ordinary
            # prose. The gate vetoes on this signal; without it the gate has no
            # non-heuristic way to tell the two apart.
            finish_reason=result.finish_reason,
        )
        # OMN-18379: the caller gets the ANSWER, not the scratchpad in front of
        # it. The gate segmented the same content with the same pure function a
        # moment ago, so the text judged and the text returned are equal by
        # construction — the alternative, stripping here only, would leave the
        # gate judging text the caller never sees. `result.txt`, the evidence
        # row and `best_content` all read this field, so one assignment covers
        # every caller-facing surface. A response with no declared boundary is
        # untouched.
        segmentation = segment_reasoning_preamble(result.content or "")
        if (
            segmentation.boundary_rule
            is not EnumReasoningBoundaryRule.NO_BOUNDARY_FOUND
        ):
            logger.info(
                "LocalDelegationDispatch: stripped %d-char reasoning preamble "
                "(rule=%s) correlation=%s",
                len(segmentation.preamble),
                segmentation.boundary_rule.value,
                correlation_id,
            )
            result = result.model_copy(update={"content": segmentation.answer})
        # OMN-7942: when a schema was declared, the CALLER gets the object, not
        # the prose around it.
        #
        # The gate is satisfied by a conforming value found anywhere in the
        # response, which is what stops the served model's untagged reasoning
        # preamble failing a correct answer. Handing that same raw text back
        # would pass the run and still leave the caller unable to parse it --
        # the register classifier of OMN-18625 would break on exactly the
        # response this path just scored 1.0. Same function, same content, so
        # the value graded and the value returned cannot differ.
        #
        # Only a value that VALIDATES is substituted. A response that fails the
        # contract is returned untouched, so a reader diagnosing a failure sees
        # what the model actually said.
        if effective_response_contract is not None and result.content:
            located = locate_schema_conforming_json(
                result.content, effective_response_contract
            )
            if located is not None and not schema_violation_reasons(
                located[0], effective_response_contract
            ):
                canonical = json.dumps(located[0])
                if canonical != result.content:
                    logger.info(
                        "LocalDelegationDispatch: returned the contract-conforming "
                        "JSON value found in a %d-char response correlation=%s",
                        len(result.content),
                        correlation_id,
                    )
                    result = result.model_copy(update={"content": canonical})
        return _AttemptOutcome(
            result=result,
            gate_result=gate_result,
            failure_message=None,
            timeout_result=None,
            preamble_chars=extraction.preamble_chars,
            output_refusal=output_refusal,
        )

    async def _evaluate_quality_gate(
        self,
        *,
        correlation_id: UUID,
        task_type: str,
        prompt: str,
        content: str,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        response_contract: dict[str, object] | None = None,
        deliverable_evidence: ModelDelegationDeliverableEvidence | None = None,
        finish_reason: EnumProviderFinishReason = EnumProviderFinishReason.ABSENT,
    ) -> ModelQualityGateResult:
        """Run the canonical quality-gate reducer, combining the LLM-judge score.

        Resolves the task-class DoD checks (``dod_deterministic`` /
        ``dod_heuristic``) from the routing authority — the SAME contract the bus
        routing reducer feeds into the gate — then evaluates the canonical
        ``delta`` reducer. When the task class declares no DoD, the reducer falls
        back to its legacy heuristic checks (refusal/empty/length), so a refusal
        still fails the gate.

        OMN-13849: for judge-combinable task classes the SAME ``HandlerJudgeAdequacy``
        EFFECT the bus quality-gate-intent handler runs
        (``handle_async`` :127-155) scores the candidate, and its
        ``judge_adequacy_score`` / ``judge_verdict`` are threaded into ``delta`` —
        so a good code answer clears the 0.85 bar on the local path exactly as it
        does on the bus. A ``JUDGE_FAILED`` verdict carries no score and falls back
        to deterministic-only (never a silent zero); the deterministic refusal/empty
        hard floor still hard-blocks before any combine.

        OMN-15193: when ``response_contract`` is supplied, ``delta`` validates the
        candidate structurally against the declared schema and REPLACES the
        task-class DoD (dod_deterministic/dod_heuristic/acceptance_criteria) and
        the judge combine for this request -- the schema is threaded through
        unconditionally, and the (still-resolved) task-class DoD stays available
        as the ``gate_input`` for the fallback branch ``delta`` takes when
        ``response_contract`` is ``None``. The judge EFFECT call is skipped when a
        contract is declared: the caller's own schema is the acceptance
        authority, so scoring the candidate against task-class judge criteria
        would be wasted work and cannot influence the verdict.

        OMN-15196: when the CALLER passes no ``response_contract`` of its own,
        the task class's own DECLARED default (``response_contract_ref`` in
        task_class_contracts.v1.yaml, e.g. the per-role dispatch report contract
        for ``agent_delegation``) is resolved and used instead -- retiring the
        keyword heuristics (``sub_tasks_verified`` et al.) those migrated classes
        used to rely on. An explicit caller-declared ``response_contract`` always
        wins over the task-class default (unchanged precedence); a task class
        that declares no ``response_contract_ref`` resolves ``None`` here exactly
        as before, so this is additive -- unmigrated classes are unaffected.
        """
        effective_response_contract = response_contract
        if effective_response_contract is None:
            effective_response_contract = resolve_task_class_response_contract(
                task_type
            )

        # OMN-16932: the prompt is threaded in so this bus-less path resolves the
        # SAME request-scoped response shape the bus routing reducer resolves. A
        # prompt that declares its own answer shape selects the contract's
        # shape_overrides heuristic band; a prompt that declares nothing resolves
        # UNCONSTRAINED and gets the class DoD unchanged.
        dod_deterministic, dod_heuristic = resolve_task_class_dod_checks(
            task_type, prompt=prompt
        )
        gate_input = ModelQualityGateInput(
            correlation_id=correlation_id,
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=dod_deterministic,
            dod_heuristic=dod_heuristic,
            quality_contract_mode=cast(EnumQualityContractMode, quality_contract_mode),
            acceptance_criteria=acceptance_criteria,
            deliverable_evidence=deliverable_evidence,
        )

        judge_score: float | None = None
        judge_verdict_value: EnumDelegationJudgeVerdict | None = None
        if (
            effective_response_contract is None
            and task_type in JUDGE_COMBINABLE_TASK_TYPES
        ):
            judge_verdict = await self._judge.score(
                correlation_id=correlation_id,
                task_type=task_type,
                prompt=(
                    "Judge whether the candidate adequately fulfills a "
                    f"{task_type} task that satisfies the declared "
                    "acceptance criteria."
                ),
                candidate_output=content,
                acceptance_criteria=acceptance_criteria,
            )
            # A judge_failed verdict carries no score — fall back to deterministic
            # only; never coerce a judge failure into a silent zero (which would
            # tank an otherwise-acceptable answer). OMN-13642: thread the verdict
            # itself (alongside the score) so a FAIL verdict vetoes acceptance in
            # the reducer even when the combined score would clear the bar.
            if judge_verdict.verdict is not EnumDelegationJudgeVerdict.JUDGE_FAILED:
                judge_score = judge_verdict.actual_score
                judge_verdict_value = judge_verdict.verdict

        # OMN-18297: the prompt is the grounding source. The gate's declared
        # identifier classes are checked against it, so a response citing a
        # pull request, sha or run id that appears nowhere in its own input
        # fails and escalates instead of scoring 1.0. Absent on the bus path
        # (ModelQualityGateIntent carries no prompt), where the gate records
        # the check as skipped rather than passed.
        return evaluate_quality_gate(
            gate_input,
            judge_adequacy_score=judge_score,
            judge_verdict=judge_verdict_value,
            response_contract=effective_response_contract,
            grounding_source=prompt,
            finish_reason=finish_reason,
        )

    def _project_evidence(
        self,
        *,
        correlation_id: UUID,
        task_type: str,
        endpoint_ref: str,
        model_id: str,
        result: ModelLlmDelegationCallResult,
        prompt: str,
        source_session_id: str | None,
        tenant_id: str | None,
        quality_passed: bool,
        failure_message: str,
        cost_usd: Decimal,
        savings_usd: Decimal,
        escalation_count: int,
        attempts: Sequence[Mapping[str, object]],
    ) -> None:
        """Materialize a delegation_events row via the canonical projection.

        Builds the delegate-skill terminal payload and runs it through the SAME
        ``HandlerProjectionDelegation`` the bus runtime uses, against the local
        SQLite projection target. Best-effort: a projection failure is logged and
        swallowed so the delegation response is never broken by an evidence write.

        OMN-13849: ``cost_usd`` is the CUMULATIVE metered spend banked across every
        attempted tier (the final tier + every rejected escalation attempt), so the
        row's cost reflects each attempt's real metered cost and never drops a
        rejected metered attempt's spend. ``escalation_count`` records how many
        up-tier re-dispatches occurred.

        OMN-18889: ``attempts`` is the per-rung ladder the caller has already
        built. It is not decoration. ``reduce_delegation_attempts`` derives the
        terminal cause FROM the ladder when one exists and only falls back to
        sniffing ``failure_message`` for quota phrasing when it is empty -- so
        omitting it here did not merely leave ``attempt_history`` empty, it
        routed every local terminal through the text fallback, where a run
        whose rungs answered and were refused on quality could be recorded as
        a provider quota failure.
        """
        payload: dict[str, object] = {
            "status": "completed" if quality_passed else "failed",
            "correlation_id": str(correlation_id),
            "task_type": task_type,
            "provider": endpoint_ref,
            "model_name": model_id,
            "prompt_text": prompt,
            "response": result.content or failure_message or "",
            "quality_gate_passed": quality_passed,
            "quality_gates_failed": [] if quality_passed else [failure_message],
            "error_message": failure_message,
            "escalation_count": escalation_count,
            "attempts": list(attempts),
            "metrics": {
                "input_tokens": result.tokens_in,
                "output_tokens": result.tokens_out,
                "total_tokens": result.tokens_in + result.tokens_out,
                "latency_ms": result.latency_ms,
                "cost_usd": float(cost_usd),
                "cost_savings_usd": float(savings_usd),
            },
        }
        # The terminal projection types session_id as UUID | None; only forward a
        # UUID-parseable value so a free-text local session id never fails the
        # evidence write (the row materializes either way).
        if source_session_id:
            try:
                payload["session_id"] = str(UUID(source_session_id))
            except ValueError:
                logger.debug(
                    "non-UUID session id %r omitted from evidence row",
                    source_session_id,
                )
        # OMN-14058 (OPERATOR-ACCEPTED INTERIM): forward the request-acceptance
        # tenant_id so the evidence row stamps a real tenant.
        #
        # OMN-16831 (operator ruling 2026-08-28, option D), item 4: this used to
        # be `if tenant_id:` -- when no tenant resolved, the key was OMITTED so
        # that the column's `DEFAULT 'omninode'` supplied one. That is a write
        # that depends on a specific isolation mechanism still being in force,
        # and it records nothing an OMN-15359 replay could route. The evidence
        # row now always carries an explicitly recorded value: the resolved
        # tenant when there is one, otherwise the house tenant slug.
        #
        # Deliberately the SLUG, not `house_tenant_write_stamp(table=...)`'s
        # table-resolved representation: this payload is not the final row.
        # It becomes `row_model.tenant_id` in
        # HandlerProjectionDelegation.project_delegate_skill_terminal, which
        # independently calls `resolve_tenant_uuid_or_none` on it to produce
        # `delegation_events`' UUID column value -- the same treatment a real
        # resolved tenant slug already receives there. Pre-resolving to the
        # UUID string here fed that UUID string back into
        # `resolve_tenant_uuid_or_none` as if it were a slug, which raises
        # (`_LEGACY_TENANT_UUID_MAP` keys are slugs, not UUID strings) --
        # caught by the full suite, not by this ticket's own narrower local
        # selection (tests/unit/nodes/node_delegate_skill_orchestrator/test_local_dispatch_evidence.py
        # and siblings, which exercise the no-configured-tenant fallback).
        #
        # OMN-18699 REPLACES the `or HOUSE_TENANT_SLUG` this line used to carry.
        # That fallback is what made 258 rows in this machine's own local store
        # indistinguishable from OmniNode's: every local run on an install with
        # no resolved tenant was recorded as the house one. `dispatch` now
        # refuses before reaching here when the install has minted no identity,
        # so `tenant_id` is a real, deployment-scoped value by the time it
        # arrives -- and the assertion below states that rather than quietly
        # re-substituting a constant if some future caller reaches this method
        # by another route. The value is the minted UUID, which
        # `resolve_registry_tenant_uuid` confirms against the local store's own
        # `tenant_registry_mirror` row (written by `onex local init`) exactly as
        # it confirms a cloud tenant against the deployed mirror.
        if not tenant_id:
            raise TenantContextMissingError(
                "OMN-18699: refusing to write a local evidence row with no "
                "tenant identity. This is unreachable through dispatch(), which "
                "refuses first; reaching it means a caller bypassed that seam. "
                "No identity will be invented or defaulted for it."
            )
        payload["tenant_id"] = tenant_id
        try:
            # The projection confirms a UUID identity against the evidence
            # store's own tenant_registry_mirror. `onex local init` writes that
            # row into the install's store; this keeps a REDIRECTED local sqlite
            # target (an overlay, a test) consistent with the install it belongs
            # to. Bounded to this install's own minted identity and to a local
            # sqlite target -- see ensure_install_identity_mirrored. INSIDE the
            # best-effort guard with the projection it serves: an evidence
            # target that cannot be written is already handled below, and
            # hoisting this out would make an unwritable store break the
            # delegation RESPONSE, which is the one thing this method promises
            # never to do.
            ensure_install_identity_mirrored(self._evidence_db)
            from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
                ModelDelegateSkillTerminalProjection,
            )

            terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
            self._projection_handler.project_delegate_skill_terminal(
                terminal, self._evidence_db
            )
        except Exception:
            logger.warning(
                "Failed to project local delegation evidence for correlation_id=%s",
                correlation_id,
                exc_info=True,
            )


class _AttemptOutcome:
    """Outcome of one dispatch attempt (effect + gate) for the escalation loop.

    Exactly one of these shapes holds:
      * ``result`` set + ``gate_result`` set — the effect succeeded and the gate
        ran; the loop inspects ``gate_result.passed`` to accept or escalate.
      * ``result`` set + ``gate_result`` None — the effect returned a transport
        failure (``result.success`` is False); the loop terminates FAILED.
      * ``result`` None + ``failure_message`` + ``timeout_result`` set — the effect
        timed out; the loop projects ``timeout_result`` and terminates FAILED.
    """

    __slots__ = (
        "failure_message",
        "gate_result",
        "output_refusal",
        "preamble_chars",
        "result",
        "timeout_result",
    )

    def __init__(
        self,
        *,
        result: ModelLlmDelegationCallResult | None,
        gate_result: ModelQualityGateResult | None,
        failure_message: str | None,
        timeout_result: ModelLlmDelegationCallResult | None,
        preamble_chars: int,
        output_refusal: ModelDelegationOutputRefusal | None,
    ) -> None:
        self.result = result
        self.gate_result = gate_result
        self.failure_message = failure_message
        self.timeout_result = timeout_result
        self.preamble_chars = preamble_chars
        self.output_refusal = output_refusal


__all__ = ["LocalDelegationDispatchPort"]
