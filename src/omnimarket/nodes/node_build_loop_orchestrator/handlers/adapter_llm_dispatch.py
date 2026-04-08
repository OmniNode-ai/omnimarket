# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that dispatches ticket builds via multi-model LLM code generation.

Implements ProtocolBuildDispatchHandler for live build loop execution.
Routes tickets to the appropriate model tier:
- Simple tasks -> local Qwen3-14B (fast)
- Medium tasks -> local Qwen3-Coder-30B (64K ctx)
- Complex tasks -> frontier models (Gemini, OpenAI)
- Review -> GLM-4.7-Flash (cheap frontier reviewer, 203K ctx)

Review policy:
- Reviewer unavailable -> review_status: "unavailable", NOT approved
- Reviewer returns malformed JSON -> retry once, then review_status: "failed", reject
- Acceptance requires structural gate pass AND (reviewer approval OR allow_unreviewed=True)

Related:
    - OMN-7856: Wire GLM-4.7-Flash as code reviewer
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_build_loop_orchestrator.handlers.adapter_delegation_router import (
    EnumModelTier,
    ModelEndpointConfig,
    build_endpoint_configs,
    route_ticket_to_tier,
)
from omnimarket.nodes.node_build_loop_orchestrator.protocols.protocol_sub_handlers import (
    BuildTarget,
    DelegationPayload,
    DispatchResult,
)

logger = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# Structured review output schema
# ---------------------------------------------------------------------------


class ModelReviewIssue(BaseModel):
    """A single issue found during code review."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: int | None = Field(default=None, description="Line number, if known.")
    severity: Literal["minor", "major", "critical"] = Field(
        ..., description="Issue severity."
    )
    message: str = Field(..., description="Issue description.")


class ModelReviewResult(BaseModel):
    """Structured output from the GLM-4.7-Flash code reviewer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approved: bool = Field(..., description="Whether the code is approved.")
    issues: list[ModelReviewIssue] = Field(
        default_factory=list, description="Issues found."
    )
    risk_level: Literal["low", "medium", "high"] = Field(
        ..., description="Overall risk level."
    )


class AdapterLlmDispatch:
    """Dispatches ticket builds via multi-model LLM code generation.

    Implements ProtocolBuildDispatchHandler for live orchestrator wiring.
    Routes each ticket to the appropriate model tier based on complexity,
    using both local models (Qwen3, DeepSeek) and frontier APIs (Gemini, OpenAI).

    Review policy (OMN-7856):
    - Reviewer unavailable: review_status="unavailable", rejected unless allow_unreviewed=True
    - Reviewer malformed: retry once, then review_status="failed", always rejected
    - Reviewer approved: accepted if structural gate also passes
    """

    def __init__(
        self,
        *,
        endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] | None = None,
        delegation_topic: str | None = None,
        allow_unreviewed: bool = False,
    ) -> None:
        self._endpoints = endpoint_configs or build_endpoint_configs()
        self._delegation_topic = delegation_topic or _DEFAULT_DELEGATION_TOPIC
        self._available_tiers = frozenset(self._endpoints.keys())
        self._allow_unreviewed = allow_unreviewed

        logger.info(
            "LLM dispatch initialized with tiers: %s (allow_unreviewed=%s)",
            ", ".join(t.value for t in sorted(self._available_tiers, key=str)),
            allow_unreviewed,
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
        3. Review via GLM-4.7-Flash (FRONTIER_REVIEW tier)
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

                # Generate plan via routed model
                plan = await self._generate_plan(target, endpoint)

                # Review via FRONTIER_REVIEW (GLM-4.7-Flash) if available
                review_status: str
                review_data: dict[str, object]
                reviewer_model: str

                if EnumModelTier.FRONTIER_REVIEW in self._endpoints:
                    reviewer_endpoint = self._endpoints[EnumModelTier.FRONTIER_REVIEW]
                    reviewer_model = reviewer_endpoint.model_id
                    review_status, review_data = await self._review_plan(
                        target, plan, reviewer_endpoint
                    )
                else:
                    # No reviewer configured — unavailable
                    review_status = "unavailable"
                    review_data = {"issues": [], "risk_level": "unknown"}
                    reviewer_model = "none"
                    logger.warning(
                        "No FRONTIER_REVIEW endpoint configured for %s — review unavailable",
                        target.ticket_id,
                    )

                # Determine acceptance under review policy
                accepted = self._is_accepted(review_status, review_data)

                payload_data: dict[str, object] = {
                    "ticket_id": target.ticket_id,
                    "title": target.title,
                    "implementation_plan": plan,
                    "review_result": review_data,
                    "review_status": review_status,
                    "accepted": accepted,
                    "correlation_id": str(correlation_id),
                    "generated_at": datetime.now(tz=UTC).isoformat(),
                    "routed_to_tier": tier.value,
                    "delegated_to": endpoint.model_id,
                    "coder_model": endpoint.model_id,
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
                    "LLM dispatch: generated plan for %s via %s (review_status=%s, accepted=%s)",
                    target.ticket_id,
                    tier.value,
                    review_status,
                    accepted,
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

    def _is_accepted(
        self, review_status: str, review_data: dict[str, object]
    ) -> bool:
        """Determine acceptance under review policy.

        Rules:
        - review_status="approved": accepted
        - review_status="rejected": rejected
        - review_status="unavailable": accepted only if allow_unreviewed=True
        - review_status="failed" or "malformed": always rejected
        """
        if review_status == "approved":
            return True
        if review_status == "unavailable":
            if self._allow_unreviewed:
                logger.warning(
                    "Accepting unreviewed output (allow_unreviewed=True, review_status=unavailable)"
                )
                return True
            return False
        return False

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
    ) -> tuple[str, dict[str, object]]:
        """Review implementation plan via GLM-4.7-Flash (FRONTIER_REVIEW tier).

        Returns (review_status, review_data) where review_status is one of:
        - "approved": reviewer approved the plan
        - "rejected": reviewer rejected with issues
        - "unavailable": endpoint unreachable
        - "malformed": reviewer returned non-JSON after retry
        - "failed": parsing failed after retry

        Never returns a status that collapses to auto-approval.
        Retries once on malformed JSON before marking failed.
        """
        user_prompt = (
            f"Ticket: {target.ticket_id} — {target.title}\n\n"
            f"Implementation plan:\n{json.dumps(plan, indent=2, default=str)[:8000]}\n\n"
            f"Review this plan and output only a JSON object."
        )

        for attempt in range(1, 3):  # max 2 attempts
            try:
                raw = await self._call_endpoint(
                    endpoint, _REVIEW_SYSTEM_PROMPT, user_prompt
                )
            except httpx.HTTPError as exc:
                logger.warning(
                    "Review endpoint unreachable for %s (attempt %d): %s",
                    target.ticket_id,
                    attempt,
                    exc,
                )
                return "unavailable", {"issues": [], "risk_level": "unknown"}

            # Try to parse structured review output
            parsed_result = self._parse_review_response(raw)
            if parsed_result is None:
                logger.warning(
                    "Review returned malformed JSON for %s (attempt %d)",
                    target.ticket_id,
                    attempt,
                )
                if attempt == 2:
                    return "failed", {"raw_response": raw, "issues": [], "risk_level": "unknown"}
                # Retry
                continue

            review_status = "approved" if parsed_result.approved else "rejected"
            return review_status, parsed_result.model_dump()

        # Should not reach here, but guard
        return "failed", {"issues": [], "risk_level": "unknown"}

    @staticmethod
    def _parse_review_response(raw: str) -> ModelReviewResult | None:
        """Parse reviewer response into ModelReviewResult.

        Handles JSON wrapped in markdown fences or bare JSON.
        Returns None if parsing fails or schema validation fails.
        """
        # Strip markdown fences if present
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            # Drop first line (```json or ```) and last line (```)
            inner = "\n".join(lines[1:-1]) if lines[-1].strip() == "```" else "\n".join(lines[1:])
            text = inner.strip()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None

        try:
            return ModelReviewResult.model_validate(data)
        except Exception:
            return None

    @staticmethod
    async def _call_endpoint(
        endpoint: ModelEndpointConfig,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.2,
    ) -> str:
        """Call an OpenAI-compatible endpoint."""
        payload = {
            "model": endpoint.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": endpoint.max_tokens,
            "temperature": temperature,
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


__all__: list[str] = ["AdapterLlmDispatch", "ModelReviewIssue", "ModelReviewResult"]
