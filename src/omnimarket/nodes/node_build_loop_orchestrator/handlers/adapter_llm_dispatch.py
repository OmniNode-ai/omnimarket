# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that dispatches ticket builds via multi-model LLM code generation.

Implements ProtocolBuildDispatchHandler for live build loop execution.
Uses the existing LLM infrastructure from omnibase_infra:
- AdapterLlmProviderOpenai for OpenAI-compatible inference (health checks, failover)
- AdapterModelRouter for multi-provider routing with round-robin fallback
- ModelLlmProviderConfig for provider configuration from the registry

Related:
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
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
    """

    def __init__(
        self,
        *,
        endpoint_configs: dict[EnumModelTier, ModelEndpointConfig] | None = None,
        delegation_topic: str | None = None,
        router: AdapterModelRouter | None = None,
    ) -> None:
        self._endpoints = endpoint_configs or build_endpoint_configs()
        self._delegation_topic = delegation_topic or _DEFAULT_DELEGATION_TOPIC
        self._router = router
        self._router_initialized = router is not None

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

        for target in targets:
            if dry_run:
                payloads.append(self._make_dry_run_payload(target, correlation_id))
                total_dispatched += 1
                continue

            try:
                # Generate plan via model router (handles failover)
                plan, coder_model = await self._generate_plan(target)

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

        return DispatchResult(
            total_dispatched=total_dispatched,
            delegation_payloads=tuple(payloads),
        )

    async def _generate_plan(
        self, target: BuildTarget
    ) -> tuple[dict[str, object], str]:
        """Generate implementation plan via the model router.

        Returns:
            Tuple of (parsed plan dict, model name used).
        """
        user_prompt = (
            f"Ticket: {target.ticket_id}\n"
            f"Title: {target.title}\n"
            f"Buildability: {target.buildability}\n\n"
            f"Generate an implementation plan."
        )

        prompt = f"{_CODER_SYSTEM_PROMPT}\n\n{user_prompt}"

        router = await self._ensure_router()
        # Use the first available provider's default model for the request
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
        model_used = response.model_used

        try:
            parsed: dict[str, object] = json.loads(response.generated_text)
            return parsed, model_used
        except json.JSONDecodeError:
            logger.warning(
                "Response not valid JSON for %s via %s, wrapping as raw",
                target.ticket_id,
                model_used,
            )
            return {
                "raw_response": response.generated_text,
                "ticket_id": target.ticket_id,
            }, model_used

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


__all__: list[str] = ["AdapterLlmDispatch"]
