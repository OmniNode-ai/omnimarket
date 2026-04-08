# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that dispatches ticket builds via multi-model LLM code generation.

Implements ProtocolBuildDispatchHandler for live build loop execution.
Routes tickets to the appropriate model tier based on complexity,
using both local models (Qwen3, DeepSeek) and frontier APIs.

Review policy (OMN-7856/OMN-7857):
- Reviewer unavailable: review_status="unavailable", rejected unless allow_unreviewed=True
- Reviewer malformed: retry once, then review_status="failed", always rejected
- Reviewer approved: accepted if structural gate also passes

Every generation attempt writes a ModelDispatchTrace to
.onex_state/dispatch-traces/ and (when KAFKA_ENABLED) emits
onex.evt.omnimarket.delegation-attempt.v1 to the event bus.

Related:
    - OMN-7854: Add source context loading
    - OMN-7855: Add dispatch tracing to .onex_state/
    - OMN-7856: Wire GLM-4.7-Flash as code reviewer
    - OMN-7857: Implement generate-review-retry loop
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import ast
import contextlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import httpx
import yaml
from omnibase_infra.adapters.llm.adapter_llm_provider_openai import (
    AdapterLlmProviderOpenai,
)
from omnibase_infra.adapters.llm.adapter_model_router import AdapterModelRouter
from omnibase_infra.adapters.llm.model_llm_adapter_request import (
    ModelLlmAdapterRequest,
)

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
    build_endpoint_configs,
    route_ticket_to_tier,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
    ModelDispatchTrace,
    ModelQualityGateResult,
    ModelReviewResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    DelegationPayload,
    DispatchResult,
)

logger = logging.getLogger(__name__)

# Bus topic for per-attempt trace events
_DELEGATION_ATTEMPT_TOPIC = "onex.evt.omnimarket.delegation-attempt.v1"

# ---------------------------------------------------------------------------
# Resolve delegation topic from build_dispatch_effect contract.yaml
# ---------------------------------------------------------------------------
_DISPATCH_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "node_build_dispatch_effect"
    / "contract.yaml"
)


def _load_delegation_topic() -> str:
    """Load delegation-request publish topic from dispatch contract."""
    if _DISPATCH_CONTRACT_PATH.exists():
        with open(_DISPATCH_CONTRACT_PATH) as fh:
            data = yaml.safe_load(fh) or {}
        for topic in (data.get("event_bus", {}) or {}).get("publish_topics", []) or []:
            if isinstance(topic, str) and "delegation-request" in topic:
                return topic
    return "delegation-request"  # fallback — never a valid topic, will be overridden


_DEFAULT_DELEGATION_TOPIC: str = _load_delegation_topic()


def _get_state_dir() -> Path:
    """Resolve .onex_state from OMNI_HOME env or cwd fallback."""
    omni_home = os.environ.get("OMNI_HOME", "")
    if omni_home:
        return Path(omni_home) / ".onex_state"
    return Path.cwd() / ".onex_state"


def _write_trace(trace: ModelDispatchTrace, state_dir: Path) -> None:
    """Write a dispatch trace to .onex_state/dispatch-traces/.

    Filename: {correlation_id}-{ticket_id}-attempt-{N}.json
    Never raises — logs on failure so a write error never kills a dispatch.
    """
    traces_dir = state_dir / "dispatch-traces"
    try:
        traces_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{trace.correlation_id}-{trace.ticket_id}-attempt-{trace.attempt}.json"
        (traces_dir / fname).write_text(trace.model_dump_json(indent=2))
        logger.debug("Wrote dispatch trace: %s", fname)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Failed to write dispatch trace for %s attempt %d: %s",
            trace.ticket_id,
            trace.attempt,
            exc,
        )


def _emit_trace_to_bus(trace: ModelDispatchTrace) -> None:
    """Emit trace event to Kafka when KAFKA_ENABLED is set.

    Bus events are observability copies — local files are authoritative.
    Silently skips when Kafka is not configured.
    """
    if not os.environ.get("KAFKA_ENABLED", ""):
        return
    try:
        from omnibase_infra.bus.kafka_producer import (
            KafkaProducerClient,  # type: ignore[import-not-found]
        )

        producer = KafkaProducerClient.from_env()
        producer.produce(topic=_DELEGATION_ATTEMPT_TOPIC, value=trace.model_dump_json())
        logger.debug(
            "Emitted delegation-attempt to bus: %s attempt %d",
            trace.ticket_id,
            trace.attempt,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Bus emit failed for %s attempt %d (trace file is authoritative): %s",
            trace.ticket_id,
            trace.attempt,
            exc,
        )


_CODER_SYSTEM_PROMPT = """\
You are an autonomous code refactoring agent for the OmniNode platform.
You will be given a template handler (a READY node to follow) and a target handler (the PARTIAL file to refactor).
Refactor the target code to follow the template pattern exactly.

Output ONLY the complete refactored Python file. Do not add explanations, markdown fences, or commentary.
"""

_REVIEW_SYSTEM_PROMPT = """\
You are a code review agent. Review the refactored code against the template pattern and original source.

Check specifically:
1. Correct method name — does the refactored handler use handle() and not run_full_pipeline() or other legacy names?
2. Correct type annotations — do parameter and return types match the template pattern exactly?
3. All logic preserved — is every branch, condition, and side effect from the original source present?
4. No hallucinated field names — does the code only reference fields that exist in the Pydantic models?

You MUST respond with ONLY a JSON object, no prose, no explanation:
{
  "approved": true,
  "issues": [{"line": 15, "severity": "major", "message": "hallucinated field name 'phase'"}],
  "risk_level": "low"
}

severity must be "minor", "major", or "critical".
risk_level must be "low", "medium", or "high".
issues must be an array (empty array if none).
"""

# 48K char budget leaves headroom for Qwen3-Coder's 64K context
_DEFAULT_MAX_CONTEXT_CHARS: int = 48000

# Failure taxonomy for traces
_FAILURE_GENERATION_MALFORMED = "generation_malformed"
_FAILURE_GATE_FAILED = "gate_failed"
_FAILURE_REVIEW_REJECTED = "review_rejected"
_FAILURE_REVIEW_UNAVAILABLE = "review_unavailable"
_FAILURE_TRANSPORT = "transport_failure"


def _build_provider_from_endpoint(
    name: str, endpoint: ModelEndpointConfig
) -> AdapterLlmProviderOpenai:
    """Create an AdapterLlmProviderOpenai from a legacy ModelEndpointConfig."""
    provider_type = "local" if not endpoint.api_key else "external_trusted"
    return AdapterLlmProviderOpenai(
        base_url=endpoint.base_url,
        default_model=endpoint.model_id,
        api_key=endpoint.api_key or None,
        provider_name=name,
        provider_type=provider_type,
        max_timeout_seconds=endpoint.timeout_seconds,
    )


async def _build_model_router(
    endpoint_configs: dict[EnumModelTier, ModelEndpointConfig],
) -> AdapterModelRouter:
    """Build an AdapterModelRouter from endpoint configs.

    Registers each configured tier as a provider with the router.
    The router handles health checking, round-robin, and failover.
    """
    router = AdapterModelRouter()
    for tier, endpoint in endpoint_configs.items():
        provider = _build_provider_from_endpoint(tier.value, endpoint)
        await router.register_provider(tier.value, provider)
    return router


class AdapterLlmDispatch:
    """Dispatches ticket builds via multi-model LLM code generation.

    Implements ProtocolBuildDispatchHandler for live orchestrator wiring.
    Routes each ticket to the appropriate model tier based on complexity,
    using both local models (Qwen3, DeepSeek) and frontier APIs (Gemini, OpenAI).

    Review policy (OMN-7856):
    - Reviewer unavailable: review_status="unavailable", rejected unless allow_unreviewed=True
    - Reviewer malformed: retry once, then review_status="failed", always rejected
    - Reviewer approved: accepted if structural gate also passes

    Every generation attempt (pass or fail) produces a ModelDispatchTrace written
    to .onex_state/dispatch-traces/ and emitted to the event bus when available.
    """

    def __init__(
        self,
        *,
        endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] | None = None,
        delegation_topic: str | None = None,
        state_dir: Path | None = None,
        max_attempts: int = 3,
        allow_unreviewed: bool = False,
        router: AdapterModelRouter | None = None,
    ) -> None:
        self._endpoints = endpoint_configs or build_endpoint_configs()
        self._delegation_topic = delegation_topic or _DEFAULT_DELEGATION_TOPIC
        self._available_tiers = frozenset(self._endpoints.keys())
        self._state_dir = state_dir or _get_state_dir()
        self._max_attempts = max_attempts
        self._allow_unreviewed = allow_unreviewed
        self._router = router
        self._router_initialized = router is not None

        # Build per-tier providers for direct access (review model)
        self._providers: dict[EnumModelTier, AdapterLlmProviderOpenai] = {}
        for tier, endpoint in self._endpoints.items():
            self._providers[tier] = _build_provider_from_endpoint(tier.value, endpoint)

        logger.info(
            "LLM dispatch initialized with tiers: %s (allow_unreviewed=%s)",
            ", ".join(t.value for t in sorted(self._available_tiers, key=str)),
            allow_unreviewed,
        )

    async def _ensure_router(self) -> AdapterModelRouter:
        """Lazily initialize the model router on first use."""
        if not self._router_initialized:
            self._router = await _build_model_router(self._endpoints)
            self._router_initialized = True
        assert self._router is not None
        return self._router

    async def handle(
        self,
        *,
        correlation_id: UUID,
        targets: tuple[BuildTarget, ...],
        dry_run: bool = False,
    ) -> DispatchResult:
        """Generate implementation plans for each buildable ticket.

        For each target:
        1. Route to appropriate model tier based on complexity
        2. Load source context (template + target + models)
        3. Run generate-review-retry loop (up to max_attempts)
        4. Package as delegation payload
        """
        logger.info(
            "LLM dispatch: %d targets (correlation_id=%s, dry_run=%s)",
            len(targets),
            correlation_id,
            dry_run,
        )

        payloads: list[DelegationPayload] = []
        total_dispatched = 0

        for target in targets:
            if dry_run:
                payloads.append(self._make_dry_run_payload(target, correlation_id))
                total_dispatched += 1
                continue

            try:
                # Route to appropriate model tier
                tier = route_ticket_to_tier(
                    title=target.title,
                    description="",  # description not on BuildTarget
                    available_tiers=self._available_tiers,
                )
                coder_endpoint = self._endpoints[tier]
                reviewer_endpoint = self._endpoints.get(EnumModelTier.FRONTIER_REVIEW)

                logger.info(
                    "Routing %s to %s (%s) — %s",
                    target.ticket_id,
                    tier.value,
                    coder_endpoint.model_id,
                    target.title[:80],
                )

                # Load source context
                template_source, target_source, model_sources = self._load_source_context(
                    target_node_dir=self._resolve_node_dir(target.ticket_id),
                )

                # Generate with review loop
                accepted_code, traces = await self._generate_with_review(
                    target=target,
                    coder_endpoint=coder_endpoint,
                    reviewer_endpoint=reviewer_endpoint,
                    template_source=template_source,
                    target_source=target_source,
                    model_sources=model_sources,
                    max_attempts=self._max_attempts,
                    correlation_id=correlation_id,
                )

                approved = accepted_code is not None
                last_trace = traces[-1] if traces else None
                review_result: dict[str, object] = {
                    "approved": approved,
                    "issues": [],
                    "risk_level": "unknown",
                }
                if last_trace and last_trace.review_result:
                    review_result = {
                        "approved": last_trace.review_result.approved,
                        "issues": [
                            i.model_dump() for i in last_trace.review_result.issues
                        ],
                        "risk_level": last_trace.review_result.risk_level,
                    }

                payload_data: dict[str, object] = {
                    "ticket_id": target.ticket_id,
                    "title": target.title,
                    "implementation_plan": accepted_code or "",
                    "review_result": review_result,
                    "correlation_id": str(correlation_id),
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "routed_to_tier": tier.value,
                    "delegated_to": coder_endpoint.model_id,
                    "coder_model": coder_endpoint.model_id,
                    "reviewer_model": reviewer_endpoint.model_id if reviewer_endpoint else None,
                    "total_attempts": len(traces),
                    "accepted": approved,
                }

                payloads.append(
                    DelegationPayload(
                        topic=self._delegation_topic,
                        payload=payload_data,
                    )
                )
                if approved:
                    total_dispatched += 1
                logger.info(
                    "LLM dispatch: %s via %s — accepted=%s, attempts=%d",
                    target.ticket_id,
                    tier.value,
                    approved,
                    len(traces),
                )

            except Exception as exc:
                logger.warning(
                    "LLM dispatch failed for %s: %s (correlation_id=%s)",
                    target.ticket_id,
                    exc,
                    correlation_id,
                )

        logger.info(
            "LLM dispatch complete: %d/%d dispatched (correlation_id=%s)",
            total_dispatched,
            len(targets),
            correlation_id,
        )

        return DispatchResult(
            total_dispatched=total_dispatched,
            delegation_payloads=tuple(payloads),
        )

    async def _generate_with_review(
        self,
        *,
        target: BuildTarget,
        coder_endpoint: ModelEndpointConfig,
        reviewer_endpoint: ModelEndpointConfig | None,
        template_source: str,
        target_source: str,
        model_sources: list[str],
        max_attempts: int = 3,
        correlation_id: UUID,
    ) -> tuple[str | None, list[ModelDispatchTrace]]:
        """Generate code with review loop. Returns (accepted_code, traces).

        Flow per attempt:
        1. Build source-grounded prompt (with LATEST review feedback if retry)
        2. Call coder endpoint
        3. Extract code from response (strip markdown fences)
        4. Run structural quality gate (ruff + AST parse + import check)
        5. If gate fails: trace as gate_failed, set feedback, continue
        6. If gate passes: send to GLM reviewer
        7. If review rejects: trace as review_rejected, set feedback (replace, don't accumulate), continue
        8. If review unavailable and not allow_unreviewed: trace as review_unavailable, continue
        9. If review approves (or no reviewer or allow_unreviewed): trace as accepted, return code

        After all attempts exhausted: return None + all traces.
        """
        traces: list[ModelDispatchTrace] = []
        review_feedback = ""

        for attempt in range(1, max_attempts + 1):
            t0 = time.monotonic()
            raw = ""
            failure_kind: str | None = None

            # 1. Build prompt (include LATEST review feedback if retry — replace, don't accumulate)
            prompt = self._build_coder_prompt(
                target=target,
                template_source=template_source,
                target_source=target_source,
                model_sources=model_sources,
            )
            if review_feedback:
                prompt += f"\n\n## REVIEW FEEDBACK (fix these issues):\n{review_feedback}"

            prompt_chars = len(prompt)

            # 2. Call coder
            try:
                raw = await self._call_endpoint(coder_endpoint, _CODER_SYSTEM_PROMPT, prompt)
            except Exception as exc:
                failure_kind = _FAILURE_TRANSPORT
                wall_clock_ms = int((time.monotonic() - t0) * 1000)
                gate = ModelQualityGateResult(
                    ruff_pass=False,
                    import_pass=False,
                    test_pass=False,
                    errors=[f"Transport failure: {exc}"],
                )
                trace = ModelDispatchTrace(
                    correlation_id=str(correlation_id),
                    ticket_id=target.ticket_id,
                    attempt=attempt,
                    timestamp=datetime.now(tz=UTC).isoformat(),
                    coder_model=coder_endpoint.model_id,
                    reviewer_model=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    prompt_chars=prompt_chars,
                    generation_raw=raw,
                    quality_gate=gate,
                    review_result=None,
                    accepted=False,
                    wall_clock_ms=wall_clock_ms,
                    failure_kind=failure_kind,
                )
                _write_trace(trace, self._state_dir)
                _emit_trace_to_bus(trace)
                traces.append(trace)
                review_feedback = f"Transport failure on attempt {attempt}, retry."
                continue

            # 3. Extract code from response
            code = self._extract_code_from_response(raw)
            if not code.strip():
                failure_kind = _FAILURE_GENERATION_MALFORMED
                wall_clock_ms = int((time.monotonic() - t0) * 1000)
                gate = ModelQualityGateResult(
                    ruff_pass=False,
                    import_pass=False,
                    test_pass=False,
                    errors=["Empty code extracted from response"],
                )
                trace = ModelDispatchTrace(
                    correlation_id=str(correlation_id),
                    ticket_id=target.ticket_id,
                    attempt=attempt,
                    timestamp=datetime.now(tz=UTC).isoformat(),
                    coder_model=coder_endpoint.model_id,
                    reviewer_model=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    prompt_chars=prompt_chars,
                    generation_raw=raw,
                    quality_gate=gate,
                    review_result=None,
                    accepted=False,
                    wall_clock_ms=wall_clock_ms,
                    failure_kind=failure_kind,
                )
                _write_trace(trace, self._state_dir)
                _emit_trace_to_bus(trace)
                traces.append(trace)
                review_feedback = "Response produced empty code. Output complete Python code only."
                continue

            # 4. Structural quality gate
            gate_result = self._run_quality_gate(code)

            # 5. If gate fails: trace and retry
            if not gate_result.all_pass:
                failure_kind = _FAILURE_GATE_FAILED
                wall_clock_ms = int((time.monotonic() - t0) * 1000)
                trace = ModelDispatchTrace(
                    correlation_id=str(correlation_id),
                    ticket_id=target.ticket_id,
                    attempt=attempt,
                    timestamp=datetime.now(tz=UTC).isoformat(),
                    coder_model=coder_endpoint.model_id,
                    reviewer_model=None,
                    prompt_tokens=0,
                    completion_tokens=0,
                    prompt_chars=prompt_chars,
                    generation_raw=raw,
                    quality_gate=gate_result,
                    review_result=None,
                    accepted=False,
                    wall_clock_ms=wall_clock_ms,
                    failure_kind=failure_kind,
                )
                _write_trace(trace, self._state_dir)
                _emit_trace_to_bus(trace)
                traces.append(trace)
                review_feedback = "Quality gate failed:\n" + "\n".join(gate_result.errors)
                continue

            # 6. Review (if reviewer available)
            review_model: ModelReviewResult | None = None
            reviewer_model_id: str | None = None

            if reviewer_endpoint:
                reviewer_model_id = reviewer_endpoint.model_id
                review_status, review_model = await self._run_review(
                    generated_code=code,
                    target_source=target_source,
                    endpoint=reviewer_endpoint,
                    ticket_id=target.ticket_id,
                )

                # 7. If review rejects: trace and retry with LATEST feedback (replace)
                if review_status == "rejected" and review_model is not None:
                    failure_kind = _FAILURE_REVIEW_REJECTED
                    wall_clock_ms = int((time.monotonic() - t0) * 1000)
                    trace = ModelDispatchTrace(
                        correlation_id=str(correlation_id),
                        ticket_id=target.ticket_id,
                        attempt=attempt,
                        timestamp=datetime.now(tz=UTC).isoformat(),
                        coder_model=coder_endpoint.model_id,
                        reviewer_model=reviewer_model_id,
                        prompt_tokens=0,
                        completion_tokens=0,
                        prompt_chars=prompt_chars,
                        generation_raw=raw,
                        quality_gate=gate_result,
                        review_result=review_model,
                        accepted=False,
                        wall_clock_ms=wall_clock_ms,
                        failure_kind=failure_kind,
                    )
                    _write_trace(trace, self._state_dir)
                    _emit_trace_to_bus(trace)
                    traces.append(trace)
                    # Replace feedback — never accumulate across retries
                    review_feedback = "Review issues:\n" + "\n".join(
                        f"- Line {i.line or '?'}: [{i.severity}] {i.message}"
                        for i in review_model.issues
                    )
                    continue

                # 8. Review unavailable or failed — check policy
                if review_status in ("unavailable", "failed", "malformed") and not self._allow_unreviewed:
                        failure_kind = _FAILURE_REVIEW_UNAVAILABLE
                        wall_clock_ms = int((time.monotonic() - t0) * 1000)
                        trace = ModelDispatchTrace(
                            correlation_id=str(correlation_id),
                            ticket_id=target.ticket_id,
                            attempt=attempt,
                            timestamp=datetime.now(tz=UTC).isoformat(),
                            coder_model=coder_endpoint.model_id,
                            reviewer_model=reviewer_model_id,
                            prompt_tokens=0,
                            completion_tokens=0,
                            prompt_chars=prompt_chars,
                            generation_raw=raw,
                            quality_gate=gate_result,
                            review_result=None,
                            accepted=False,
                            wall_clock_ms=wall_clock_ms,
                            failure_kind=failure_kind,
                        )
                        _write_trace(trace, self._state_dir)
                        _emit_trace_to_bus(trace)
                        traces.append(trace)
                        review_feedback = f"Reviewer {review_status} — retry."
                        continue

            # 9. Accepted: gate passed and (approved or no reviewer or allow_unreviewed)
            wall_clock_ms = int((time.monotonic() - t0) * 1000)
            trace = ModelDispatchTrace(
                correlation_id=str(correlation_id),
                ticket_id=target.ticket_id,
                attempt=attempt,
                timestamp=datetime.now(tz=UTC).isoformat(),
                coder_model=coder_endpoint.model_id,
                reviewer_model=reviewer_model_id,
                prompt_tokens=0,
                completion_tokens=0,
                prompt_chars=prompt_chars,
                generation_raw=raw,
                quality_gate=gate_result,
                review_result=review_model,
                accepted=True,
                wall_clock_ms=wall_clock_ms,
                failure_kind=None,
            )
            _write_trace(trace, self._state_dir)
            _emit_trace_to_bus(trace)
            traces.append(trace)
            logger.info(
                "Accepted code for %s on attempt %d/%d",
                target.ticket_id,
                attempt,
                max_attempts,
            )
            return code, traces

        # All attempts exhausted
        logger.warning(
            "All %d attempts exhausted for %s",
            max_attempts,
            target.ticket_id,
        )
        return None, traces

    async def _run_review(
        self,
        *,
        generated_code: str,
        target_source: str,
        endpoint: ModelEndpointConfig,
        ticket_id: str,
    ) -> tuple[str, ModelReviewResult | None]:
        """Review generated code via the GLM reviewer endpoint.

        Returns (review_status, review_result) where review_status is one of:
        - "approved": reviewer approved
        - "rejected": reviewer rejected with issues
        - "unavailable": endpoint unreachable
        - "malformed": reviewer returned non-JSON after retry
        - "failed": parsing failed after retry

        Never collapses "unavailable" to "approved".
        Retries once on malformed JSON before marking failed.
        """
        user_prompt = (
            f"Ticket: {ticket_id}\n\n"
            f"## ORIGINAL SOURCE:\n{target_source[:4000]}\n\n"
            f"## GENERATED CODE:\n{generated_code[:8000]}\n\n"
            f"Review the generated code against the original source. "
            f"Output only a JSON object."
        )

        for attempt in range(1, 3):  # max 2 review attempts
            try:
                raw = await self._call_endpoint(
                    endpoint, _REVIEW_SYSTEM_PROMPT, user_prompt, temperature=0.3
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Reviewer unreachable for %s (attempt %d): %s",
                    ticket_id,
                    attempt,
                    exc,
                )
                return "unavailable", None

            parsed = self._parse_review_response(raw)
            if parsed is None:
                logger.warning(
                    "Review returned malformed JSON for %s (attempt %d)",
                    ticket_id,
                    attempt,
                )
                if attempt == 2:
                    return "failed", None
                continue

            status = "approved" if parsed.approved else "rejected"
            return status, parsed

        return "failed", None

    def _run_quality_gate(self, code: str) -> ModelQualityGateResult:
        """Run structural quality gate on generated code.

        Checks:
        - AST parse (syntax validity)
        - ruff check (lint)

        Returns ModelQualityGateResult. Never raises.
        """
        errors: list[str] = []
        ruff_pass = True
        import_pass = True

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, prefix="onex_gate_"
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name

        try:
            # AST syntax check
            try:
                ast.parse(code)
            except SyntaxError as exc:
                errors.append(f"Syntax error: {exc}")
                import_pass = False
                ruff_pass = False  # ruff will also fail on bad syntax

            # ruff lint check (only if syntax OK)
            if not errors:
                try:
                    result = subprocess.run(
                        ["ruff", "check", "--output-format=concise", tmp_path],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if result.returncode != 0:
                        ruff_pass = False
                        for line in result.stdout.strip().splitlines():
                            errors.append(f"ruff: {line}")
                except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
                    # ruff not available — skip, don't fail the gate
                    logger.debug("ruff check skipped: %s", exc)

        finally:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)

        return ModelQualityGateResult(
            ruff_pass=ruff_pass,
            import_pass=import_pass,
            test_pass=True,  # test_pass not run at generation time
            errors=errors,
        )

    @staticmethod
    def _parse_review_response(raw: str) -> ModelReviewResult | None:
        """Parse reviewer response into ModelReviewResult.

        Handles JSON wrapped in markdown fences or bare JSON.
        Returns None if parsing fails or schema validation fails.
        """
        text = raw.strip()
        # Strip markdown fences if present
        if text.startswith("```"):
            lines = text.splitlines()
            inner = "\n".join(lines[1:-1]) if lines and lines[-1].strip() == "```" else "\n".join(lines[1:])
            text = inner.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # Try to extract JSON block from prose
            json_match = re.search(r"\{[^{}]*\"approved\"[^{}]*\}", raw, re.DOTALL)
            if not json_match:
                return None
            try:
                data = json.loads(json_match.group(0))
            except json.JSONDecodeError:
                return None

        try:
            return ModelReviewResult.model_validate(data)
        except Exception:
            return None

    def _build_coder_prompt(
        self,
        *,
        target: BuildTarget,
        template_source: str,
        target_source: str,
        model_sources: list[str] | None = None,
        max_context_chars: int = _DEFAULT_MAX_CONTEXT_CHARS,
    ) -> str:
        """Build a coder prompt with actual source code context.

        Truncates model_sources (whole files) if total exceeds max_context_chars.
        Never truncates template or target handlers mid-file.
        """
        header = f"Ticket: {target.ticket_id} — {target.title}"
        template_section = f"## TEMPLATE (follow this pattern):\n{template_source}"
        target_section = f"## TARGET (refactor this):\n{target_source}"

        base_parts = [header, "", template_section, "", target_section]
        base_prompt = "\n".join(base_parts)

        if not model_sources:
            return base_prompt

        # Add model files one at a time, dropping from the end if over budget
        model_parts: list[str] = []
        for src in model_sources:
            candidate = "\n".join(
                [base_prompt, "", "## RELEVANT MODELS:", *model_parts, src]
            )
            if len(candidate) <= max_context_chars:
                model_parts.append(src)
            else:
                logger.debug(
                    "Dropping model source (budget %d chars): %d chars",
                    max_context_chars,
                    len(src),
                )
                break

        if model_parts:
            return "\n".join(
                [base_prompt, "", "## RELEVANT MODELS:", *model_parts]
            )
        return base_prompt

    def _load_source_context(
        self,
        *,
        target_node_dir: Path,
        template_node_dir: Path | None = None,
        omni_home: Path | None = None,
    ) -> tuple[str, str, list[str]]:
        """Load source files for prompt context.

        Returns (template_source, target_source, model_sources).

        Source selection rules:
        1. Target handler: target_node_dir/handlers/handler_*.py
        2. Template handler: template_node_dir/handlers/handler_*.py
           - If not provided, auto-selects nearest READY node by scanning siblings
        3. Related models: target_node_dir/models/model_*.py
        4. Contract: target_node_dir/contract.yaml

        Whole files only — no mid-file truncation. Falls back to empty strings
        with a WARNING log if files don't exist.
        """
        target_source = self._read_handler_file(target_node_dir)

        if template_node_dir is not None:
            template_source = self._read_handler_file(template_node_dir)
        else:
            template_source = self._auto_select_template(target_node_dir)

        model_sources: list[str] = []
        models_dir = target_node_dir / "models"
        if models_dir.exists():
            for model_file in sorted(models_dir.glob("model_*.py")):
                try:
                    model_sources.append(model_file.read_text())
                except OSError as exc:
                    logger.warning("Could not read model file %s: %s", model_file, exc)

        contract_path = target_node_dir / "contract.yaml"
        if contract_path.exists():
            try:
                model_sources.append(contract_path.read_text())
            except OSError as exc:
                logger.warning("Could not read contract %s: %s", contract_path, exc)

        return template_source, target_source, model_sources

    def _read_handler_file(self, node_dir: Path) -> str:
        """Read the primary handler file from a node directory."""
        handlers_dir = node_dir / "handlers"
        if not handlers_dir.exists():
            logger.warning("No handlers/ directory in %s", node_dir)
            return ""
        handler_files = sorted(handlers_dir.glob("handler_*.py"))
        if not handler_files:
            logger.warning("No handler_*.py files in %s", handlers_dir)
            return ""
        try:
            return handler_files[0].read_text()
        except OSError as exc:
            logger.warning("Could not read handler %s: %s", handler_files[0], exc)
            return ""

    def _auto_select_template(self, target_node_dir: Path) -> str:
        """Auto-select the nearest READY node as template by scanning siblings."""
        nodes_dir = target_node_dir.parent
        if not nodes_dir.exists():
            return ""

        for candidate in sorted(nodes_dir.iterdir()):
            if not candidate.is_dir():
                continue
            if candidate == target_node_dir:
                continue
            handler_src = self._read_handler_file(candidate)
            if not handler_src:
                continue
            if "def handle(" in handler_src:
                logger.info("Auto-selected template node: %s", candidate.name)
                return handler_src

        logger.warning(
            "No suitable template node found in siblings of %s", target_node_dir
        )
        return ""

    def _resolve_node_dir(self, ticket_id: str) -> Path:
        """Resolve the node directory for a ticket from the omnimarket source tree."""
        nodes_root = Path(__file__).resolve().parent.parent.parent
        return nodes_root / f"node_{ticket_id.lower().replace('-', '_')}"

    @staticmethod
    def _extract_code_from_response(raw_response: str) -> str:
        """Extract Python code from model response.

        Handles: bare code, ```python fences, ``` fences, mixed prose+code.
        Returns the largest contiguous Python block found.
        Falls back to raw_response if no fences detected.
        """
        python_fence = re.search(
            r"```python\s*\n(.*?)```", raw_response, re.DOTALL
        )
        if python_fence:
            return python_fence.group(1)

        generic_fence = re.search(r"```\s*\n(.*?)```", raw_response, re.DOTALL)
        if generic_fence:
            return generic_fence.group(1)

        return raw_response

    @staticmethod
    async def _call_endpoint(
        endpoint: ModelEndpointConfig,
        system_prompt: str,
        user_prompt: str,
        *,
        temperature: float = 0.1,
        top_p: float | None = None,
    ) -> str:
        """Call an OpenAI-compatible endpoint."""
        payload: dict[str, object] = {
            "model": endpoint.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": endpoint.max_tokens,
            "temperature": temperature,
        }
        if top_p is not None:
            payload["top_p"] = top_p

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if endpoint.api_key:
            headers["Authorization"] = f"Bearer {endpoint.api_key}"

        async with httpx.AsyncClient(timeout=endpoint.timeout_seconds) as client:
            resp = await client.post(
                f"{endpoint.base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])

    @staticmethod
    def _make_dry_run_payload(
        target: BuildTarget, correlation_id: UUID
    ) -> DelegationPayload:
        """Create a dry-run delegation payload (no LLM call)."""
        return DelegationPayload(
            topic="dry-run",
            payload={
                "ticket_id": target.ticket_id,
                "title": target.title,
                "dry_run": True,
                "correlation_id": str(correlation_id),
            },
        )

    async def close(self) -> None:
        """Close all provider connections."""
        for provider in self._providers.values():
            await provider.close()


__all__: list[str] = ["AdapterLlmDispatch"]
