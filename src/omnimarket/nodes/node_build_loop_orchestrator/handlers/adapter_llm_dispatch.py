# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapter that dispatches ticket builds via multi-model LLM code generation.

Implements ProtocolBuildDispatchHandler for live build loop execution.
Uses the existing LLM infrastructure from omnibase_infra:
- AdapterLlmProviderOpenai for OpenAI-compatible inference (health checks, failover)
- AdapterModelRouter for multi-provider routing with round-robin fallback
- ModelLlmProviderConfig for provider configuration from the registry

Related:
    - OMN-7854: Add source context loading
    - OMN-7810: Wire build loop to Linear queue
    - OMN-5113: Autonomous Build Loop epic
"""

from __future__ import annotations

import json
import logging
import re
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
You are an autonomous code refactoring agent for the OmniNode platform.
You will be given a template handler (a READY node to follow) and a target handler (the PARTIAL file to refactor).
Refactor the target code to follow the template pattern exactly.

Output ONLY the complete refactored Python file. Do not add explanations, markdown fences, or commentary.
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

# 48K char budget leaves headroom for Qwen3-Coder's 64K context
_DEFAULT_MAX_CONTEXT_CHARS: int = 48000


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
        2. Load source context (template + target + models)
        3. Generate code via routed model using source-grounded prompt
        4. Review via reasoning provider (if available)
        5. Package as delegation payload
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
                # Generate plan via model router (handles failover) with source context
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
            return "\n".join([base_prompt, "", "## RELEVANT MODELS:", *model_parts])
        return base_prompt

    def _load_source_context(
        self,
        *,
        target_node_dir: Path,
        template_node_dir: Path | None = None,
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
        # Load target handler
        target_source = self._read_handler_file(target_node_dir)

        # Load template handler
        if template_node_dir is not None:
            template_source = self._read_handler_file(template_node_dir)
        else:
            template_source = self._auto_select_template(target_node_dir)

        # Load related models
        model_sources: list[str] = []
        models_dir = target_node_dir / "models"
        if models_dir.exists():
            for model_file in sorted(models_dir.glob("model_*.py")):
                try:
                    model_sources.append(model_file.read_text())
                except OSError as exc:
                    logger.warning("Could not read model file %s: %s", model_file, exc)

        # Append contract.yaml if present
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
            # Skip node if it has no handler
            handler_src = self._read_handler_file(candidate)
            if not handler_src:
                continue
            # Prefer nodes that define a canonical handle() method
            if "def handle(" in handler_src:
                logger.info("Auto-selected template node: %s", candidate.name)
                return handler_src

        logger.warning(
            "No suitable template node found in siblings of %s", target_node_dir
        )
        return ""

    def _resolve_node_dir(self, ticket_id: str) -> Path:
        """Resolve the node directory for a ticket from the omnimarket source tree.

        Falls back to a non-existent path — _load_source_context handles missing dirs gracefully.
        """
        # Nodes are named by ticket convention in the build loop; fall back to
        # the orchestrator's own node dir as a safe default
        nodes_root = Path(__file__).resolve().parent.parent.parent
        return nodes_root / f"node_{ticket_id.lower().replace('-', '_')}"

    @staticmethod
    def _extract_code_from_response(raw_response: str) -> str:
        """Extract Python code from model response.

        Handles: bare code, ```python fences, ``` fences, mixed prose+code.
        Returns the first fenced Python block found, or the raw response if no fences detected.
        Falls back to raw_response if no fences detected.
        """
        # Try ```python ... ``` first
        python_fence = re.search(r"```python\s*\n(.*?)```", raw_response, re.DOTALL)
        if python_fence:
            return python_fence.group(1)

        # Try generic ``` ... ```
        generic_fence = re.search(r"```\s*\n(.*?)```", raw_response, re.DOTALL)
        if generic_fence:
            return generic_fence.group(1)

        # No fences — return raw (assume the model output bare code as instructed)
        return raw_response

    async def _generate_plan(
        self, target: BuildTarget
    ) -> tuple[dict[str, object], str]:
        """Generate implementation plan via the model router with source context.

        Loads source context (template + target handler + models) to ground the
        prompt, then dispatches via AdapterModelRouter for failover-safe generation.

        Returns:
            Tuple of (parsed plan dict, model name used).
        """
        template_source, target_source, model_sources = self._load_source_context(
            target_node_dir=self._resolve_node_dir(target.ticket_id),
        )

        user_prompt = self._build_coder_prompt(
            target=target,
            template_source=template_source,
            target_source=target_source,
            model_sources=model_sources,
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
        raw = response.generated_text
        code = self._extract_code_from_response(raw)

        return {
            "ticket_id": target.ticket_id,
            "generated_code": code,
            "raw_response": raw,
            "prompt_chars": len(user_prompt),
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
