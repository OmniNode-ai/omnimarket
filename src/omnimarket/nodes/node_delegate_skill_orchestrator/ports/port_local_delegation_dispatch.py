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
import logging
import multiprocessing
import queue
import sys
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumQualityContractMode,
    ModelQualityGateInput,
)

from omnimarket.config import get_settings
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.events.delegation_judge_verdict import EnumDelegationJudgeVerdict
from omnimarket.inference.protocol_config import apply_inference_protocol

# The reducer (``delta``) returns the omnimarket wire result DTO (it carries the
# P1 deterministic-acceptance evidence fields not yet promoted to core), so the
# port annotates against that surface rather than the core re-export.
from omnimarket.models.delegation.wire.model_quality_gate import (
    SCORE_SOURCE_DETERMINISTIC_ACCEPTANCE,
    ModelQualityGateResult,
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
    first_eligible_tier,
    is_free_tier,
    next_eligible_tier,
    resolve_task_class_dod_checks,
    resolve_task_class_max_escalations,
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
from omnimarket.routing.delegation_backend_resolution import (
    ModelResolvedDelegationBackend,
    resolve_delegation_backend,
    resolve_effective_max_tokens,
    resolve_timeout_seconds,
)
from omnimarket.routing.roi_overlay import (
    ModelRoutingRoiOverlay,
    resolve_roi_overlay,
)

logger = logging.getLogger(__name__)

_RESERVED_PROVIDER_REQUEST_KEYS = frozenset(
    {"model", "messages", "max_tokens", "temperature"}
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
            EnumDelegationFailureClass.INVALID_JSON,
        }
    )
)


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

type _EffectHandler = Callable[
    [ModelLlmDelegationCallRequest], ModelLlmDelegationCallResult
]
type _EffectWorkerMessage = (
    tuple[Literal["ok"], ModelLlmDelegationCallResult]
    | tuple[Literal["error"], str, str]
)


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
    """Run the sync effect in a child process and return exactly one message."""
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
    """Run the blocking sync effect behind a process boundary with a hard kill."""
    context: Any = _resolve_effect_process_context()
    result_queue = context.Queue()
    process = context.Process(
        target=_effect_handler_worker,
        args=(effect_handler, request, result_queue),
        daemon=True,
    )
    process.start()

    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            message = _read_effect_worker_message(result_queue)
            if message is not None:
                process.join(timeout=_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS)
                if message[0] == "ok":
                    return message[1]
                raise RuntimeError(f"{message[1]}: {message[2]}")

            if not process.is_alive():
                process.join(timeout=_EFFECT_PROCESS_TERMINATE_GRACE_SECONDS)
                message = _read_effect_worker_message(result_queue)
                if message is not None:
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
                raise TimeoutError
            await asyncio.sleep(min(_EFFECT_PROCESS_POLL_INTERVAL_SECONDS, remaining))
    finally:
        if process.is_alive():
            _terminate_effect_process(process)
        result_queue.close()
        result_queue.join_thread()


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
        """Resolve the ROI overlay from ``roi_db`` — fail-OPEN (OMN-14001).

        Returns None when no ROI projection adapter was injected (the local
        default), or on ANY read error, so a captured-outcome read never breaks a
        live delegation. When a ``roi_db`` carrying ``context_roi_scores`` IS
        provided (the runtime / live-proof wiring), the real per-tier success rates
        drive tier suppression. ``tier_for_backend`` maps each row's
        ``endpoint_ref`` to its routing tier.
        """
        if self._roi_db is None:
            return None
        try:
            return resolve_roi_overlay(
                self._roi_db,
                task_type=task_type,
                tier_of_endpoint=tier_for_backend,
            )
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
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
        tenant_id: str | None,
    ) -> dict[str, object]:
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
        # Falls back to ONEX_TENANT_ID, mirroring the bus orchestrator's
        # HandlerDelegationWorkflow.handle_delegation_request. No tenant identity
        # otherwise exists on this bus-less local CLI path, so evidence rows
        # would silently land under the 'omninode' column default. The durable
        # per-tenant identity design is OMN-14107.
        resolved_tenant_id = tenant_id or get_settings().onex_tenant_id or None

        # 1. ROUTING AUTHORITY — resolve the INITIAL (cheapest-first) backend.
        #    Cheapest-first among tiers NOT ROI-suppressed; escalation only advances
        #    UP the closed-set task-class tier_order (OMN-13140/OMN-13849/OMN-14001).
        backend = self._resolve_initial_backend(task_type, roi_overlay=roi_overlay)

        # Escalation budget from the task-class contract escalation_policy
        # (OMN-13849). None -> the class declares no budget; fall back to the bus
        # orchestrator's default so both paths escalate the same bounded count.
        max_escalations = resolve_task_class_max_escalations(task_type)
        if max_escalations is None:
            max_escalations = _DEFAULT_MAX_ESCALATIONS

        # Tiers already attempted (excluded from re-selection), mirroring the bus
        # ``excluded_tiers`` set threaded into ``next_eligible_tier``.
        excluded_tiers: set[str] = set()
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
            attempt_outcome = await self._run_single_attempt(
                backend=backend,
                prompt=prompt,
                task_type=task_type,
                correlation_id=correlation_id,
                max_tokens=max_tokens,
                quality_contract_mode=quality_contract_mode,
                acceptance_criteria=acceptance_criteria,
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
                current_tier = tier_for_backend(backend.backend_id) or backend.tier
                excluded_tiers.add(current_tier)

                escalated_backend: ModelResolvedDelegationBackend | None = None
                if (
                    _is_retryable_transport_failure(transport_failure_class)
                    and escalation_count < max_escalations
                ):
                    escalated_backend = self._resolve_next_backend(
                        current_tier=current_tier,
                        task_type=task_type,
                        excluded_tiers=frozenset(excluded_tiers),
                        roi_overlay=roi_overlay,
                    )

                # A transport failure never runs the quality gate, so bank its
                # (typically zero) metered cost directly — mirrors the
                # post-success banking below without requiring a gate verdict.
                cumulative_cost_usd += transport_result.actual_cost_usd
                cumulative_savings_usd += transport_result.savings_usd
                attempts.append(
                    {
                        "tier": backend.tier,
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
                    }
                )

                if escalated_backend is not None:
                    logger.info(
                        "LocalDelegationDispatch: escalating task_type=%s from "
                        "tier=%s to tier=%s on transport failure_class=%s "
                        "(attempt %d/%d) correlation=%s reason=%s",
                        task_type,
                        current_tier,
                        escalated_backend.tier,
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
                )
                return {
                    "status": "failed",
                    # OMN-14220: return the best authored artifact seen so far rather
                    # than nothing — a final transport failure (e.g. 429) must not
                    # discard a correct earlier-tier authorship.
                    "content": best_content,
                    "error_message": transport_failure_message,
                    "correlation_id": str(correlation_id),
                    "delegated_to": backend.endpoint_ref,
                    "model_name": backend.model_id,
                    "escalation_count": escalation_count,
                    "cost_usd": float(cumulative_cost_usd),
                    "attempts": attempts,
                }

            result = transport_result

            # This attempt's inference ran and incurred real metered cost — bank it
            # BEFORE deciding pass/fail so a rejected metered tier's spend is
            # counted even if we escalate away from it (OMN-13849).
            cumulative_cost_usd += result.actual_cost_usd
            cumulative_savings_usd += result.savings_usd

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
                    backend.tier,
                    float(result.actual_cost_usd),
                    float(cumulative_cost_usd),
                    escalation_count,
                    correlation_id,
                )

            gate_result = attempt_outcome.gate_result
            assert gate_result is not None
            quality_passed = self._is_quality_accepted(task_type, gate_result)
            attempts.append(
                {
                    "tier": backend.tier,
                    "backend_id": backend.backend_id,
                    "model_id": backend.model_id,
                    "quality_gate_passed": quality_passed,
                    "quality_score": gate_result.quality_score,
                    "cost_usd": float(result.actual_cost_usd),
                }
            )

            # OMN-14220: remember the best (highest-scoring) non-empty artifact so a
            # terminal failure can return real authored work instead of discarding it.
            if result.content and gate_result.quality_score > best_content_score:
                best_content = result.content
                best_content_score = gate_result.quality_score

            if quality_passed:
                # 5. PROJECTION — materialize the local evidence row from the
                #    terminal, carrying the REAL gate verdict and the CUMULATIVE
                #    metered cost across every attempt (never a hardcoded PASS,
                #    never a dropped rejected-attempt cost).
                self._project_evidence(
                    correlation_id=correlation_id,
                    task_type=task_type,
                    endpoint_ref=backend.endpoint_ref,
                    model_id=backend.model_id,
                    result=result,
                    prompt=prompt,
                    source_session_id=source_session_id,
                    tenant_id=resolved_tenant_id,
                    quality_passed=True,
                    failure_message="",
                    cost_usd=cumulative_cost_usd,
                    savings_usd=cumulative_savings_usd,
                    escalation_count=escalation_count,
                )
                return {
                    "status": "completed",
                    "content": result.content or "",
                    "delegated_to": backend.endpoint_ref,
                    "model_name": backend.model_id,
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
                }

            # --- Quality-gate FAIL: evaluate escalation (mirror bus loop) -------
            gate_failure_message = "; ".join(gate_result.failure_reasons)
            current_tier = tier_for_backend(backend.backend_id) or backend.tier

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
                backend.tier,
                correlation_id,
                gate_failure_message,
                result.content or "",
            )

            next_backend: ModelResolvedDelegationBackend | None = None
            if escalation_count < max_escalations:
                next_backend = self._resolve_next_backend(
                    current_tier=current_tier,
                    task_type=task_type,
                    excluded_tiers=frozenset(excluded_tiers),
                    roi_overlay=roi_overlay,
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
                )
                return {
                    "status": "failed",
                    # OMN-14220: prefer the best authored artifact across all attempts
                    # (highest gate score, non-empty) over the LAST attempt's content —
                    # a later tier that scored lower (or returned empty) must not
                    # overwrite a correct earlier authorship the gate (falsely) rejected.
                    "content": best_content or (result.content or ""),
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
                }

            # Escalate: advance to the next tier's backend and retry.
            logger.info(
                "LocalDelegationDispatch: escalating task_type=%s from tier=%s to "
                "tier=%s (attempt %d/%d) correlation=%s reason=%s",
                task_type,
                current_tier,
                next_backend.tier,
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

    def _resolve_initial_backend(
        self,
        task_type: str,
        *,
        roi_overlay: ModelRoutingRoiOverlay | None = None,
    ) -> ModelResolvedDelegationBackend:
        """Resolve the cheapest-first INITIAL backend via the task-class tier_order.

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

    def _resolve_next_backend(
        self,
        *,
        current_tier: str,
        task_type: str,
        excluded_tiers: frozenset[str],
        roi_overlay: ModelRoutingRoiOverlay | None = None,
    ) -> ModelResolvedDelegationBackend | None:
        """Resolve the next eligible tier's backend, or None if none exists.

        OMN-14001: ``roi_overlay`` (when set) demotes ROI-suppressed tiers on the
        escalation hop too, with the overlay's fail-safe second pass keeping the
        ladder reachable when suppression would exhaust it.

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
            current_tier, excluded_tiers, task_type=task_type, roi_overlay=roi_overlay
        )
        if next_tier is None:
            return None
        backend_id = backend_id_for_tier(next_tier, task_type)
        if backend_id is None:
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
    ) -> _AttemptOutcome:
        """Run one resolve->effect->gate attempt for ``backend``.

        Returns the effect result + gate verdict on a completed call, or a
        canonical TIMEOUT/transport failure marker the caller projects. This is
        exactly the single-shot behavior the port had before OMN-13849; the
        escalation loop calls it once per tier.
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
        system_prompt = _TASK_TYPE_SYSTEM_PROMPTS.get(
            task_type, _TASK_TYPE_SYSTEM_PROMPTS["research"]
        )
        (
            outbound_system_prompt,
            outbound_prompt,
            provider_request_options,
        ) = apply_inference_protocol(
            system_prompt=system_prompt,
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
            endpoint_ref=backend.endpoint_ref,
            prompt=outbound_prompt,
            prompt_hash="",
            system_prompt=outbound_system_prompt,
            task_type=task_type,
            max_tokens=effective_max_tokens,
            timeout_seconds=timeout_seconds,
            model_tier=backend.tier,
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
            )

        if not result.success:
            return _AttemptOutcome(
                result=result,
                gate_result=None,
                failure_message=None,
                timeout_result=None,
            )

        # 4. CANONICAL QUALITY GATE (OMN-13597) — run the SAME reducer the bus
        #    path runs. HTTP/transport success is NOT a quality verdict: a model
        #    refusal or empty answer returns success here but must NOT be recorded
        #    as a gate PASS. Resolve the task-class DoD checks from the routing
        #    authority and evaluate the real verdict + graded score, threading the
        #    LLM-judge adequacy score for combinable task classes (OMN-13849).
        gate_result = await self._evaluate_quality_gate(
            correlation_id=correlation_id,
            task_type=task_type,
            content=result.content or "",
            quality_contract_mode=quality_contract_mode,
            acceptance_criteria=acceptance_criteria,
        )
        return _AttemptOutcome(
            result=result,
            gate_result=gate_result,
            failure_message=None,
            timeout_result=None,
        )

    async def _evaluate_quality_gate(
        self,
        *,
        correlation_id: UUID,
        task_type: str,
        content: str,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
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
        """
        dod_deterministic, dod_heuristic = resolve_task_class_dod_checks(task_type)
        gate_input = ModelQualityGateInput(
            correlation_id=correlation_id,
            task_type=task_type,
            llm_response_content=content,
            dod_deterministic=dod_deterministic,
            dod_heuristic=dod_heuristic,
            quality_contract_mode=cast(EnumQualityContractMode, quality_contract_mode),
            acceptance_criteria=acceptance_criteria,
        )

        judge_score: float | None = None
        judge_verdict_value: EnumDelegationJudgeVerdict | None = None
        if task_type in JUDGE_COMBINABLE_TASK_TYPES:
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

        return evaluate_quality_gate(
            gate_input,
            judge_adequacy_score=judge_score,
            judge_verdict=judge_verdict_value,
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
        # tenant_id so the evidence row stamps a real tenant instead of the
        # 'omninode' column default. None (no ONEX_TENANT_ID configured) omits
        # the key so the column default still applies.
        if tenant_id:
            payload["tenant_id"] = tenant_id
        try:
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

    __slots__ = ("failure_message", "gate_result", "result", "timeout_result")

    def __init__(
        self,
        *,
        result: ModelLlmDelegationCallResult | None,
        gate_result: ModelQualityGateResult | None,
        failure_message: str | None,
        timeout_result: ModelLlmDelegationCallResult | None,
    ) -> None:
        self.result = result
        self.gate_result = gate_result
        self.failure_message = failure_message
        self.timeout_result = timeout_result


__all__ = ["LocalDelegationDispatchPort"]
