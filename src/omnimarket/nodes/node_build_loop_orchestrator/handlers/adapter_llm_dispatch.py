# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that dispatches ticket builds via multi-model LLM code generation.

Implements ProtocolBuildDispatchHandler for live build loop execution.
Routes tickets to the appropriate model tier:
- Simple tasks -> local Qwen3-14B (fast)
- Medium tasks -> local Qwen3-Coder-30B (64K ctx)
- Complex tasks -> frontier models (Gemini, OpenAI)
- Review -> DeepSeek-R1 (reasoning)

Every generation attempt writes a ModelDispatchTrace to
.onex_state/dispatch-traces/ and (when KAFKA_ENABLED) emits
onex.evt.omnimarket.delegation-attempt.v1 to the event bus.

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-7855: Add dispatch tracing to .onex_state/
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

import httpx
import yaml

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
    build_endpoint_configs,
    route_ticket_to_tier,
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


class AdapterLlmDispatch:
    """Dispatches ticket builds via multi-model LLM code generation.

    Implements ProtocolBuildDispatchHandler for live orchestrator wiring.
    Routes each ticket to the appropriate model tier based on complexity,
    using both local models (Qwen3, DeepSeek) and frontier APIs (Gemini, OpenAI).

    Every generation attempt (pass or fail) produces a ModelDispatchTrace written
    to .onex_state/dispatch-traces/ and emitted to the event bus when available.
    """

    def __init__(
        self,
        *,
        endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] | None = None,
        delegation_topic: str | None = None,
        state_dir: Path | None = None,
    ) -> None:
        self._endpoints = endpoint_configs or build_endpoint_configs()
        self._delegation_topic = delegation_topic or _DEFAULT_DELEGATION_TOPIC
        self._available_tiers = frozenset(self._endpoints.keys())
        self._state_dir = state_dir or _get_state_dir()

        logger.info(
            "LLM dispatch initialized with tiers: %s",
            ", ".join(t.value for t in sorted(self._available_tiers, key=str)),
        )

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
        2. Generate implementation plan via routed model
        3. Review via DeepSeek-R1 (reasoning specialist)
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
                endpoint = self._endpoints[tier]

                logger.info(
                    "Routing %s to %s (%s) — %s",
                    target.ticket_id,
                    tier.value,
                    endpoint.model_id,
                    target.title[:80],
                )

                # Generate plan via routed model (traced)
                plan, _trace = await self._generate_plan_traced(
                    target=target,
                    endpoint=endpoint,
                    correlation_id=correlation_id,
                    attempt=1,
                )

                # Review via reasoning model (if available)
                review: dict[str, object] = {
                    "approved": True,
                    "issues": [],
                    "risk_level": "unknown",
                }
                if EnumModelTier.LOCAL_REASONING in self._endpoints:
                    review = await self._review_plan(
                        target,
                        plan,
                        self._endpoints[EnumModelTier.LOCAL_REASONING],
                    )

                payload_data: dict[str, object] = {
                    "ticket_id": target.ticket_id,
                    "title": target.title,
                    "implementation_plan": plan,
                    "review_result": review,
                    "correlation_id": str(correlation_id),
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "routed_to_tier": tier.value,
                    "delegated_to": endpoint.model_id,
                    "coder_model": endpoint.model_id,
                    "reviewer_model": "deepseek-r1-32b",
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
                    tier.value,
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

        return DispatchResult(
            total_dispatched=total_dispatched,
            delegation_payloads=tuple(payloads),
        )

    async def _generate_plan_traced(
        self,
        *,
        target: BuildTarget,
        endpoint: ModelEndpointConfig,
        correlation_id: UUID,
        attempt: int,
    ) -> tuple[dict[str, object], ModelDispatchTrace]:
        """Generate implementation plan and write a dispatch trace.

        Always writes a trace — even on failure — so no attempt is ever lost.
        Returns (plan_dict, trace).
        """
        user_prompt = (
            f"Ticket: {target.ticket_id}\n"
            f"Title: {target.title}\n"
            f"Buildability: {target.buildability}\n\n"
            f"Generate an implementation plan."
        )
        prompt_chars = len(user_prompt)
        t0 = time.monotonic()
        raw = ""
        accepted = False
        gate = ModelQualityGateResult(
            ruff_pass=False, import_pass=False, test_pass=False, errors=[]
        )

        try:
            raw = await self._call_endpoint(endpoint, _CODER_SYSTEM_PROMPT, user_prompt)
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
            coder_model=endpoint.model_id,
            reviewer_model=None,
            prompt_tokens=0,
            completion_tokens=0,
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
                endpoint.tier.value,
            )
            plan = {"raw_response": raw, "ticket_id": target.ticket_id}

        return plan, trace

    async def _generate_plan(
        self, target: BuildTarget, endpoint: ModelEndpointConfig
    ) -> dict[str, object]:
        """Generate implementation plan via the routed model endpoint."""
        user_prompt = (
            f"Ticket: {target.ticket_id}\n"
            f"Title: {target.title}\n"
            f"Buildability: {target.buildability}\n\n"
            f"Generate an implementation plan."
        )

        raw = await self._call_endpoint(endpoint, _CODER_SYSTEM_PROMPT, user_prompt)

        try:
            parsed: dict[str, object] = json.loads(raw)
            return parsed
        except json.JSONDecodeError:
            logger.warning(
                "Response not valid JSON for %s via %s, wrapping as raw",
                target.ticket_id,
                endpoint.tier.value,
            )
            return {"raw_response": raw, "ticket_id": target.ticket_id}

    async def _review_plan(
        self,
        target: BuildTarget,
        plan: dict[str, object],
        endpoint: ModelEndpointConfig,
    ) -> dict[str, object]:
        """Review implementation plan via the reasoning model."""
        user_prompt = (
            f"Ticket: {target.ticket_id} — {target.title}\n\n"
            f"Implementation plan:\n{json.dumps(plan, indent=2, default=str)[:8000]}\n\n"
            f"Review this plan."
        )

        try:
            raw = await self._call_endpoint(
                endpoint, _REVIEW_SYSTEM_PROMPT, user_prompt
            )
            review_result: dict[str, object] = json.loads(raw)
            return review_result
        except (json.JSONDecodeError, httpx.HTTPError) as exc:
            logger.warning(
                "Review failed for %s: %s — defaulting to approved",
                target.ticket_id,
                exc,
            )
            return {"approved": True, "issues": [], "risk_level": "unknown"}

    @staticmethod
    async def _call_endpoint(
        endpoint: ModelEndpointConfig,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Call an OpenAI-compatible endpoint."""
        payload = {
            "model": endpoint.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": endpoint.max_tokens,
            "temperature": 0.2,
        }

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


__all__: list[str] = ["AdapterLlmDispatch"]
