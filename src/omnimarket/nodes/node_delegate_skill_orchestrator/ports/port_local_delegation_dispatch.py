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
     against a local SQLite projection target so the local delegation tail is
     still materialized (the deprecated DirectCurl port's bespoke sqlite write is
     replaced by the same canonical projection the bus runtime uses).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import cast
from uuid import UUID

from omnibase_core.models.delegation.wire import (
    EnumQualityContractMode,
    ModelQualityGateInput,
)

from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.inference.protocol_config import apply_inference_protocol

# The reducer (``delta``) returns the omnimarket wire result DTO (it carries the
# P1 deterministic-acceptance evidence fields not yet promoted to core), so the
# port annotates against that surface rather than the core re-export.
from omnimarket.models.delegation.wire.model_quality_gate import (
    ModelQualityGateResult,
)

# Canonical quality-gate reducer (OMN-13597): the SAME gate the bus path runs.
# ``delta`` is the pure reducer; ``resolve_task_class_dod_checks`` is the routing
# authority's public DoD resolver. Composing both here makes the local CLI path
# run the real gate instead of recording a hardcoded PASS — a refusal or empty
# answer now projects ``quality_gate_passed=false``.
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as evaluate_quality_gate,
)
from omnimarket.nodes.node_delegation_routing_reducer.handlers.handler_delegation_routing import (
    resolve_task_class_dod_checks,
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
from omnimarket.projection.sqlite_database import (
    SqliteDatabaseAdapter,
    default_evidence_db_path,
)
from omnimarket.routing.delegation_backend_resolution import (
    resolve_delegation_backend,
    resolve_effective_max_tokens,
    resolve_timeout_seconds,
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

# OMN-13597: hard ceiling buffer (seconds) added to the contract-resolved
# per-backend transport timeout when bounding the blocking effect call. Covers
# the synchronous health probe that precedes the LLM POST so a stalled connect
# can never hang the local CLI past ``transport_timeout + buffer``.
_DISPATCH_TIMEOUT_BUFFER_SECONDS = 10.0


class LocalDelegationDispatchPort:
    """Resolve routing, run the canonical effect, and project evidence in-process.

    Default port when no event_bus is provided (local ``onex delegate``). Owns no
    transport detail — the effect handler selects curl/httpx by runtime profile.
    """

    def __init__(
        self,
        *,
        effect_handler: HandlerLlmDelegationCall | None = None,
        projection_handler: HandlerProjectionDelegation | None = None,
        evidence_db: DatabaseAdapter | None = None,
        evidence_db_path: Path | None = None,
    ) -> None:
        self._effect_handler = effect_handler or HandlerLlmDelegationCall()
        self._projection_handler = projection_handler or HandlerProjectionDelegation()
        self._evidence_db: DatabaseAdapter = evidence_db or SqliteDatabaseAdapter(
            evidence_db_path or default_evidence_db_path()
        )

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
    ) -> dict[str, object]:
        # 1. ROUTING AUTHORITY — resolve model_id + COMPLETE endpoint_ref +
        #    the per-backend output-token budget (OMN-13161).
        backend = resolve_delegation_backend(task_type)

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
        # Fix: (1) offload the blocking call to a worker thread so the loop stays
        # responsive, and (2) wrap it in a hard ``asyncio.wait_for`` ceiling so
        # ``dispatch`` ALWAYS returns and ALWAYS writes a trustworthy evidence row
        # within ``transport_timeout + buffer``. The ceiling is the
        # contract-resolved per-backend timeout plus a fixed buffer that covers
        # the preceding health probe.
        #
        # Note on process exit (CodeRabbit, OMN-13597): ``asyncio.wait_for``
        # cannot cancel an already-running ``to_thread`` worker, so ``asyncio.run``
        # still joins that worker at loop shutdown. That join is bounded — once the
        # loop is no longer frozen, the transport's OWN timeout (curl ``--max-time``
        # / httpx ``timeout``, threaded from ``timeout_seconds``) terminates the
        # call. The pre-fix infinite hang was the FROZEN loop preventing BOTH
        # timers from ever firing; with the loop responsive the worst case is one
        # bounded ``timeout_seconds`` join after ``dispatch`` has already returned
        # its failed verdict and written the evidence row.
        dispatch_deadline_seconds = timeout_seconds + _DISPATCH_TIMEOUT_BUFFER_SECONDS
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(self._effect_handler, call_request),
                timeout=dispatch_deadline_seconds,
            )
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
            self._project_evidence(
                correlation_id=correlation_id,
                task_type=task_type,
                endpoint_ref=backend.endpoint_ref,
                model_id=backend.model_id,
                result=timeout_result,
                prompt=prompt,
                source_session_id=source_session_id,
                quality_passed=False,
                failure_message=failure_message,
            )
            return {
                "status": "failed",
                "error_message": failure_message,
                "correlation_id": str(correlation_id),
                "delegated_to": backend.endpoint_ref,
                "model_name": backend.model_id,
            }

        if not result.success:
            failure_message = result.error_message or "delegation call failed"
            self._project_evidence(
                correlation_id=correlation_id,
                task_type=task_type,
                endpoint_ref=backend.endpoint_ref,
                model_id=backend.model_id,
                result=result,
                prompt=prompt,
                source_session_id=source_session_id,
                quality_passed=False,
                failure_message=failure_message,
            )
            return {
                "status": "failed",
                "error_message": failure_message,
                "correlation_id": str(correlation_id),
                "delegated_to": backend.endpoint_ref,
                "model_name": backend.model_id,
            }

        # 4. CANONICAL QUALITY GATE (OMN-13597) — run the SAME reducer the bus
        #    path runs. HTTP/transport success is NOT a quality verdict: a model
        #    refusal or empty answer returns success here but must NOT be recorded
        #    as a gate PASS. Resolve the task-class DoD checks from the routing
        #    authority and evaluate the real verdict + graded score.
        gate_result = self._evaluate_quality_gate(
            correlation_id=correlation_id,
            task_type=task_type,
            content=result.content or "",
            quality_contract_mode=quality_contract_mode,
            acceptance_criteria=acceptance_criteria,
        )
        quality_passed = gate_result.passed
        quality_score = gate_result.quality_score
        gate_failure_message = (
            "; ".join(gate_result.failure_reasons) if not quality_passed else ""
        )

        # 5. PROJECTION — materialize the local evidence row from the terminal,
        #    carrying the REAL gate verdict (never a hardcoded PASS).
        self._project_evidence(
            correlation_id=correlation_id,
            task_type=task_type,
            endpoint_ref=backend.endpoint_ref,
            model_id=backend.model_id,
            result=result,
            prompt=prompt,
            source_session_id=source_session_id,
            quality_passed=quality_passed,
            failure_message=gate_failure_message,
        )

        return {
            "status": "completed" if quality_passed else "failed",
            "content": result.content or "",
            "delegated_to": backend.endpoint_ref,
            "model_name": backend.model_id,
            "quality_gate_passed": quality_passed,
            "quality_score": quality_score,
            "quality_gates_failed": list(gate_result.failure_reasons),
            "delegation_latency_ms": result.latency_ms,
            "input_tokens": result.tokens_in,
            "output_tokens": result.tokens_out,
            "total_tokens": result.tokens_in + result.tokens_out,
            "correlation_id": str(correlation_id),
        }

    def _evaluate_quality_gate(
        self,
        *,
        correlation_id: UUID,
        task_type: str,
        content: str,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> ModelQualityGateResult:
        """Run the canonical quality-gate reducer for a successful local call.

        Resolves the task-class DoD checks (``dod_deterministic`` /
        ``dod_heuristic``) from the routing authority — the SAME contract the bus
        routing reducer feeds into the gate — then evaluates the canonical
        ``delta`` reducer. When the task class declares no DoD, the reducer falls
        back to its legacy heuristic checks (refusal/empty/length), so a refusal
        still fails the gate. No judge adequacy score is available on the local
        path, so it is omitted (deterministic + heuristic checks still apply).
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
        return evaluate_quality_gate(gate_input)

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
        quality_passed: bool,
        failure_message: str,
    ) -> None:
        """Materialize a delegation_events row via the canonical projection.

        Builds the delegate-skill terminal payload and runs it through the SAME
        ``HandlerProjectionDelegation`` the bus runtime uses, against the local
        SQLite projection target. Best-effort: a projection failure is logged and
        swallowed so the delegation response is never broken by an evidence write.
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
            "metrics": {
                "input_tokens": result.tokens_in,
                "output_tokens": result.tokens_out,
                "total_tokens": result.tokens_in + result.tokens_out,
                "latency_ms": result.latency_ms,
                "cost_usd": float(result.actual_cost_usd),
                "cost_savings_usd": float(result.savings_usd),
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


__all__ = ["LocalDelegationDispatchPort"]
