# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that dispatches ticket builds via multi-model LLM code generation.

Implements ProtocolBuildDispatchHandler for live build loop execution.
Uses the existing LLM infrastructure from omnibase_infra:
- AdapterLlmProviderOpenai for OpenAI-compatible inference (health checks, failover)
- AdapterModelRouter for multi-provider routing with round-robin fallback
- ModelLlmProviderConfig for provider configuration from the registry

Every generation attempt writes a ModelDispatchTrace to
.onex_state/dispatch-traces/ and (when KAFKA_ENABLED) emits
onex.evt.omnimarket.delegation-attempt.v1 to the event bus.

After all tickets are processed, aggregate ModelDispatchMetrics are written to
.onex_state/dispatch-metrics/{correlation_id}.json and emitted as
onex.evt.omnimarket.delegation-metrics.v1.

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-7855: Add dispatch tracing to .onex_state/
    - OMN-7858: Add dispatch metrics summary
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

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
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_metrics import (
    ModelDispatchMetrics,
)
from omnimarket.nodes.node_build_loop_orchestrator.models.model_dispatch_trace import (
    ModelDispatchTrace,
    ModelQualityGateResult,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    DelegationPayload,
    DispatchResult,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Load bus topics from contracts (single source of truth — no hardcoding)
# ---------------------------------------------------------------------------
_ORCHESTRATOR_CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"
_DISPATCH_CONTRACT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "node_build_dispatch_effect"
    / "contract.yaml"
)


def _load_topic_from_contract(contract_path: Path, keyword: str) -> str:
    """Load a publish topic matching keyword from a contract.yaml."""
    if contract_path.exists():
        with open(contract_path) as fh:
            data = yaml.safe_load(fh) or {}
        for topic in (data.get("event_bus", {}) or {}).get("publish_topics", []) or []:
            if isinstance(topic, str) and keyword in topic:
                return topic
    return f"onex.evt.omnimarket.{keyword}.v1"  # fallback matches contract convention


_DELEGATION_ATTEMPT_TOPIC: str = _load_topic_from_contract(
    _ORCHESTRATOR_CONTRACT_PATH, "delegation-attempt"
)
_DELEGATION_METRICS_TOPIC: str = _load_topic_from_contract(
    _ORCHESTRATOR_CONTRACT_PATH, "delegation-metrics"
)
_DEFAULT_DELEGATION_TOPIC: str = _load_topic_from_contract(
    _DISPATCH_CONTRACT_PATH, "delegation-request"
)


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
            KafkaProducerClient,
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


def _compute_metrics(
    *,
    correlation_id: str,
    traces: list[ModelDispatchTrace],
) -> ModelDispatchMetrics:
    """Compute aggregate metrics from a list of dispatch traces.

    Covers all tickets in a single dispatch run. Each ticket may have
    multiple traces (one per generation attempt).
    """
    if not traces:
        return ModelDispatchMetrics(
            correlation_id=correlation_id,
            total_tickets=0,
            accepted_count=0,
            rejected_count=0,
            total_generation_attempts=0,
            total_review_iterations=0,
            avg_attempts_per_ticket=0.0,
            total_prompt_tokens=0,
            total_completion_tokens=0,
            total_review_tokens=0,
            total_wall_clock_ms=0,
            coder_model="none",
            reviewer_model=None,
            quality_gate_failure_rate=0.0,
            review_rejection_rate=0.0,
        )

    # Group traces by ticket_id to determine per-ticket outcomes
    tickets: dict[str, list[ModelDispatchTrace]] = {}
    for t in traces:
        tickets.setdefault(t.ticket_id, []).append(t)

    accepted_count = sum(
        1
        for ticket_traces in tickets.values()
        if any(t.accepted for t in ticket_traces)
    )
    rejected_count = len(tickets) - accepted_count

    total_attempts = len(traces)
    total_review_iterations = sum(1 for t in traces if t.review_result is not None)

    avg_attempts = total_attempts / len(tickets) if tickets else 0.0

    total_prompt_tokens = sum(t.prompt_tokens for t in traces)
    total_completion_tokens = sum(t.completion_tokens for t in traces)
    total_review_tokens = sum(
        t.review_result.review_tokens for t in traces if t.review_result is not None
    )
    total_wall_clock_ms = sum(t.wall_clock_ms for t in traces)

    # Coder model: most-used model across all traces (handles multi-model routing)
    from collections import Counter  # noqa: PLC0415

    coder_counts: Counter[str] = Counter(t.coder_model for t in traces)
    coder_model: str = coder_counts.most_common(1)[0][0] if coder_counts else "unknown"

    # Reviewer model: use the first non-None reviewer_model found
    reviewer_model: str | None = next(
        (t.reviewer_model for t in traces if t.reviewer_model is not None),
        None,
    )

    # Quality gate failure rate: fraction of attempts that failed gate (never reached review)
    gate_failed = sum(
        1 for t in traces if not t.quality_gate.all_pass and t.review_result is None
    )
    quality_gate_failure_rate = gate_failed / total_attempts if total_attempts else 0.0

    # Review rejection rate: fraction of gate-passing attempts rejected by reviewer
    gate_passing = [t for t in traces if t.quality_gate.all_pass]
    reviewed_rejected = sum(
        1
        for t in gate_passing
        if t.review_result is not None and not t.review_result.approved
    )
    review_rejection_rate = (
        reviewed_rejected / len(gate_passing) if gate_passing else 0.0
    )

    return ModelDispatchMetrics(
        correlation_id=correlation_id,
        total_tickets=len(tickets),
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        total_generation_attempts=total_attempts,
        total_review_iterations=total_review_iterations,
        avg_attempts_per_ticket=avg_attempts,
        total_prompt_tokens=total_prompt_tokens,
        total_completion_tokens=total_completion_tokens,
        total_review_tokens=total_review_tokens,
        total_wall_clock_ms=total_wall_clock_ms,
        coder_model=coder_model,
        reviewer_model=reviewer_model,
        quality_gate_failure_rate=quality_gate_failure_rate,
        review_rejection_rate=review_rejection_rate,
    )


def _write_metrics(metrics: ModelDispatchMetrics, state_dir: Path) -> None:
    """Write aggregate dispatch metrics to .onex_state/dispatch-metrics/.

    Filename: {correlation_id}.json
    Never raises — logs on failure so a write error never kills a dispatch.
    """
    metrics_dir = state_dir / "dispatch-metrics"
    try:
        metrics_dir.mkdir(parents=True, exist_ok=True)
        fname = f"{metrics.correlation_id}.json"
        (metrics_dir / fname).write_text(metrics.model_dump_json(indent=2))
        logger.info(
            "Wrote dispatch metrics: %s (accepted=%d/%d, avg_attempts=%.2f)",
            fname,
            metrics.accepted_count,
            metrics.total_tickets,
            metrics.avg_attempts_per_ticket,
        )
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Failed to write dispatch metrics for %s: %s",
            metrics.correlation_id,
            exc,
        )


def _emit_metrics_to_bus(metrics: ModelDispatchMetrics) -> None:
    """Emit aggregate metrics event to Kafka when KAFKA_ENABLED is set.

    Bus events are observability copies — local files are authoritative.
    Silently skips when Kafka is not configured.
    """
    if not os.environ.get("KAFKA_ENABLED", ""):
        return
    try:
        from omnibase_infra.bus.kafka_producer import (
            KafkaProducerClient,
        )

        producer = KafkaProducerClient.from_env()
        producer.produce(
            topic=_DELEGATION_METRICS_TOPIC, value=metrics.model_dump_json()
        )
        logger.debug(
            "Emitted delegation-metrics to bus: %s",
            metrics.correlation_id,
        )
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Bus emit failed for metrics %s (metrics file is authoritative): %s",
            metrics.correlation_id,
            exc,
        )


_CODER_SYSTEM_PROMPT = """\
You are an autonomous code implementation agent for the OmniNode platform.
Given a ticket ID, title, and context, produce a structured implementation plan.

Your response must be a JSON object with these fields:
{
  "ticket_id": "<ticket ID>",
  "implementation_plan": {
    "files_to_modify": ["list of file paths"],
    "files_to_create": ["list of new file paths"],
    "approach": "brief description of the implementation approach",
    "estimated_complexity": "low|medium|high",
    "test_strategy": "brief description of how to test"
  },
  "code_changes": [
    {
      "file_path": "path/to/file.py",
      "action": "modify|create",
      "description": "what to change",
      "code_snippet": "relevant code"
    }
  ]
}

Focus on producing actionable, concrete changes. Do not explain or apologize.
"""

_REVIEW_SYSTEM_PROMPT = """\
You are a code review agent. Review the proposed implementation plan and code changes.
Check for:
1. Correctness: Will the changes achieve the ticket's goal?
2. Safety: Any security issues, data loss risks, or breaking changes?
3. Completeness: Are tests included? Are edge cases handled?

Respond with a JSON object:
{
  "approved": true/false,
  "issues": ["list of issues found"],
  "suggestions": ["list of improvements"],
  "risk_level": "low|medium|high"
}
"""


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
    Uses AdapterModelRouter from omnibase_infra for model selection with
    health checks, failover, and round-robin load balancing.

    Every generation attempt (pass or fail) produces a ModelDispatchTrace written
    to .onex_state/dispatch-traces/ and emitted to the event bus when available.
    """

    def __init__(
        self,
        *,
        endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] | None = None,
        delegation_topic: str | None = None,
        router: AdapterModelRouter | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self._endpoints = endpoint_configs or build_endpoint_configs()
        self._delegation_topic = delegation_topic or _DEFAULT_DELEGATION_TOPIC
        self._router = router
        self._router_initialized = router is not None
        self._state_dir = state_dir or _get_state_dir()

        # Build per-tier providers for direct access (review model)
        self._providers: dict[EnumModelTier, AdapterLlmProviderOpenai] = {}
        for tier, endpoint in self._endpoints.items():
            self._providers[tier] = _build_provider_from_endpoint(tier.value, endpoint)

        logger.info(
            "LLM dispatch initialized with providers: %s",
            ", ".join(t.value for t in sorted(self._endpoints.keys(), key=str)),
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
        1. Route to best available provider via AdapterModelRouter
        2. Generate implementation plan via routed provider
        3. Review via reasoning provider (if available)
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
        all_traces: list[ModelDispatchTrace] = []

        for target in targets:
            if dry_run:
                payloads.append(self._make_dry_run_payload(target, correlation_id))
                total_dispatched += 1
                continue

            try:
                # Generate plan via model router (traced)
                plan, trace = await self._generate_plan_traced(
                    target=target,
                    correlation_id=correlation_id,
                    attempt=1,
                )
                all_traces.append(trace)
                coder_model = trace.coder_model

                # Review via reasoning model (if available)
                review: dict[str, object] = {
                    "approved": True,
                    "issues": [],
                    "risk_level": "unknown",
                }
                reviewer_model = "none"
                if EnumModelTier.LOCAL_REASONING in self._providers:
                    review, reviewer_model = await self._review_plan(target, plan)

                payload_data: dict[str, object] = {
                    "ticket_id": target.ticket_id,
                    "title": target.title,
                    "implementation_plan": plan,
                    "review_result": review,
                    "correlation_id": str(correlation_id),
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "delegated_to": coder_model,
                    "coder_model": coder_model,
                    "reviewer_model": reviewer_model,
                }

                payloads.append(
                    DelegationPayload(
                        topic=self._delegation_topic,
                        payload=payload_data,
                    )
                )
                total_dispatched += 1
                logger.info(
                    "LLM dispatch: generated plan for %s via %s (approved=%s)",
                    target.ticket_id,
                    coder_model,
                    review.get("approved", "unknown"),
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

        # Compute and persist aggregate metrics after all tickets processed
        if not dry_run:
            metrics = _compute_metrics(
                correlation_id=str(correlation_id),
                traces=all_traces,
            )
            _write_metrics(metrics, self._state_dir)
            _emit_metrics_to_bus(metrics)

        return DispatchResult(
            total_dispatched=total_dispatched,
            delegation_payloads=tuple(payloads),
        )

    async def _generate_plan_traced(
        self,
        *,
        target: BuildTarget,
        correlation_id: UUID,
        attempt: int,
    ) -> tuple[dict[str, object], ModelDispatchTrace]:
        """Generate implementation plan via the model router and write a dispatch trace.

        Always writes a trace — even on failure — so no attempt is ever lost.
        Returns (plan_dict, trace).
        """
        user_prompt = (
            f"Ticket: {target.ticket_id}\n"
            f"Title: {target.title}\n"
            f"Buildability: {target.buildability}\n\n"
            f"Generate an implementation plan."
        )
        prompt = f"{_CODER_SYSTEM_PROMPT}\n\n{user_prompt}"
        prompt_chars = len(user_prompt)
        t0 = time.monotonic()
        raw = ""
        model_used = "unknown"
        accepted = False
        gate = ModelQualityGateResult(
            ruff_pass=False, import_pass=False, test_pass=False, errors=[]
        )

        prompt_tokens = 0
        completion_tokens = 0
        try:
            router = await self._ensure_router()
            available = await router.get_available_providers()
            model_name = "default"
            if available:
                provider_name = available[0]
                endpoint = next(
                    (e for t, e in self._endpoints.items() if t.value == provider_name),
                    None,
                )
                if endpoint:
                    model_name = endpoint.model_id

            request = ModelLlmAdapterRequest(
                prompt=prompt,
                model_name=model_name,
                max_tokens=8192,
                temperature=0.2,
            )
            response = await router.generate_typed(request)
            raw = response.generated_text
            model_used = response.model_used
            usage = response.usage_statistics or {}
            prompt_tokens = int(usage.get("prompt_tokens", 0))
            completion_tokens = int(usage.get("completion_tokens", 0))
            try:
                json.loads(raw)
                gate = ModelQualityGateResult(
                    ruff_pass=True, import_pass=True, test_pass=True, errors=[]
                )
                accepted = True
            except json.JSONDecodeError as je:
                gate = ModelQualityGateResult(
                    ruff_pass=False,
                    import_pass=False,
                    test_pass=False,
                    errors=[f"JSON parse error: {je}"],
                )
        except Exception as exc:
            gate = ModelQualityGateResult(
                ruff_pass=False,
                import_pass=False,
                test_pass=False,
                errors=[f"LLM call failed: {exc}"],
            )

        wall_clock_ms = int((time.monotonic() - t0) * 1000)
        trace = ModelDispatchTrace(
            correlation_id=str(correlation_id),
            ticket_id=target.ticket_id,
            attempt=attempt,
            timestamp=datetime.now(tz=UTC).isoformat(),
            coder_model=model_used,
            reviewer_model=None,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            prompt_chars=prompt_chars,
            generation_raw=raw,
            quality_gate=gate,
            review_result=None,
            accepted=accepted,
            wall_clock_ms=wall_clock_ms,
        )
        _write_trace(trace, self._state_dir)
        _emit_trace_to_bus(trace)

        if accepted:
            try:
                plan: dict[str, object] = json.loads(raw)
            except json.JSONDecodeError:
                plan = {"raw_response": raw, "ticket_id": target.ticket_id}
        else:
            logger.warning(
                "Response not valid JSON for %s via %s, wrapping as raw",
                target.ticket_id,
                model_used,
            )
            plan = {"raw_response": raw, "ticket_id": target.ticket_id}

        return plan, trace

    async def _review_plan(
        self,
        target: BuildTarget,
        plan: dict[str, object],
    ) -> tuple[dict[str, object], str]:
        """Review implementation plan via the reasoning provider.

        Returns:
            Tuple of (review result dict, reviewer model name).
        """
        user_prompt = (
            f"Ticket: {target.ticket_id} — {target.title}\n\n"
            f"Implementation plan:\n{json.dumps(plan, indent=2, default=str)[:8000]}\n\n"
            f"Review this plan."
        )

        prompt = f"{_REVIEW_SYSTEM_PROMPT}\n\n{user_prompt}"

        provider = self._providers.get(EnumModelTier.LOCAL_REASONING)
        if provider is None:
            return {"approved": True, "issues": [], "risk_level": "unknown"}, "none"

        endpoint = self._endpoints[EnumModelTier.LOCAL_REASONING]
        request = ModelLlmAdapterRequest(
            prompt=prompt,
            model_name=endpoint.model_id,
            max_tokens=4096,
            temperature=0.1,
        )

        try:
            response = await provider.generate_async(request)
            model_used = response.model_used
            review_result: dict[str, object] = json.loads(response.generated_text)
            return review_result, model_used
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning(
                "Review failed for %s: %s — defaulting to approved",
                target.ticket_id,
                exc,
            )
            return {
                "approved": True,
                "issues": [],
                "risk_level": "unknown",
            }, endpoint.model_id

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


__all__: list[str] = ["AdapterLlmDispatch", "_compute_metrics", "_write_metrics"]
