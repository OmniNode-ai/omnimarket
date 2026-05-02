# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAbCompareOrchestrator — fans out inference to N models in parallel.

Reads models_registry.yaml at construction time. At handle() time:
  1. Resolves env vars; skips models with missing required API key.
  2. Fans out asyncio.gather() calls to HandlerLlmOpenaiCompatible from omnibase_infra.
  3. Calculates cost from registry pricing fields.
  4. Returns ModelAbCompareResult with comparison rows sorted by cost.

All LLM I/O is delegated to HandlerLlmOpenaiCompatible (omnibase_infra), which
handles retries, circuit breaking, auth, and error classification. The orchestrator
never makes direct HTTP calls or imports httpx.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_result import (
    ModelAbCompareResult,
    ModelComparisonRow,
)
from omnimarket.nodes.node_ab_compare_orchestrator.models.model_ab_compare_start import (
    ModelAbCompareStart,
)

logger = logging.getLogger(__name__)

_REGISTRY_PATH = Path(__file__).parent.parent / "models_registry.yaml"
_DEFAULT_SYSTEM_PROMPT = (
    "You are a expert software engineer. Respond with clean, working Python code only."
)


class _ResolvedModel(BaseModel):
    """A registry entry after env var resolution — ready to call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    display_name: str
    endpoint_url: str
    protocol: str
    model_id_resolved: str
    cost_per_1k_input: float
    cost_per_1k_output: float
    location: str
    context_window: int
    api_key: str | None = None


def _load_registry() -> list[dict[str, Any]]:
    return yaml.safe_load(_REGISTRY_PATH.read_text())["models"]  # type: ignore[no-any-return]


def _resolve_models(
    registry: list[dict[str, Any]],
    requested: list[str],
) -> tuple[list[_ResolvedModel], list[str]]:
    """Resolve env vars, apply key guards. Returns (available, skipped_ids)."""
    resolved: list[_ResolvedModel] = []
    skipped: list[str] = []

    for entry in registry:
        model_id: str = entry["id"]

        if requested != ["all"] and model_id not in requested:
            continue

        # Key guard for cloud models
        requires_key: str | None = entry.get("requires_key")
        api_key: str | None = None
        if requires_key:
            api_key = os.environ.get(requires_key)
            if not api_key:
                logger.info("Skipping %s: missing env var %s", model_id, requires_key)
                skipped.append(model_id)
                continue

        # Only openai_compatible protocol supported via HandlerLlmOpenaiCompatible
        protocol = entry.get("protocol", "")
        if protocol != "openai_compatible":
            logger.info("Skipping %s: unsupported protocol %s", model_id, protocol)
            skipped.append(model_id)
            continue

        endpoint_url = entry.get("endpoint", "")
        if not endpoint_url:
            logger.info("Skipping %s: no endpoint declared", model_id)
            skipped.append(model_id)
            continue

        model_id_resolved = entry.get(
            "model_id", entry.get("model_id_default", model_id)
        )

        resolved.append(
            _ResolvedModel(
                model_id=model_id,
                display_name=entry["display_name"],
                endpoint_url=endpoint_url,
                protocol=protocol,
                model_id_resolved=model_id_resolved,
                cost_per_1k_input=float(entry["cost_per_1k_input"]),
                cost_per_1k_output=float(entry["cost_per_1k_output"]),
                location=entry["location"],
                context_window=int(entry["context_window"]),
                api_key=api_key,
            )
        )

    return resolved, skipped


def _run_quality_check(output: str) -> str:
    """Run ruff check on code output, return 'pass' or 'fail:<reason>'."""
    try:
        with tempfile.NamedTemporaryFile(suffix=".py", mode="w", delete=False) as f:
            f.write(output)
            fname = f.name
        result = subprocess.run(
            ["ruff", "check", "--select=E,F", fname],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        Path(fname).unlink(missing_ok=True)
        return (
            "pass" if result.returncode == 0 else f"fail:{result.stdout[:120].strip()}"
        )
    except Exception as exc:
        return f"skip:{exc}"


def _calculate_cost(
    model: _ResolvedModel, prompt_tokens: int, completion_tokens: int
) -> float:
    return (
        prompt_tokens * model.cost_per_1k_input
        + completion_tokens * model.cost_per_1k_output
    ) / 1000.0


def _create_transport() -> Any:
    """Create a MixinLlmHttpTransport instance for HandlerLlmOpenaiCompatible."""
    from omnibase_infra.mixins.mixin_llm_http_transport import MixinLlmHttpTransport

    class _Transport(MixinLlmHttpTransport):
        def __init__(self) -> None:
            self._init_llm_http_transport(target_name="ab-compare-orchestrator")

    return _Transport()


class HandlerAbCompareOrchestrator:
    """Orchestrator: loads registry, fans out to HandlerLlmOpenaiCompatible, collects, emits.

    Uses HandlerLlmOpenaiCompatible from omnibase_infra for all LLM I/O.
    No direct HTTP calls, no httpx imports in this module.
    """

    def __init__(self, effect_handler: Any | None = None) -> None:
        """Initialize the orchestrator.

        Args:
            effect_handler: Injectable handler for testing. Must implement
                ``async def handle(request: ModelLlmInferenceRequest) -> ModelLlmInferenceResponse``.
                When None, creates a HandlerLlmOpenaiCompatible with a transport.
        """
        if effect_handler is not None:
            self._effect = effect_handler
        else:
            from omnibase_infra.nodes.node_llm_inference_effect.handlers.handler_llm_openai_compatible import (
                HandlerLlmOpenaiCompatible,
            )

            transport = _create_transport()
            self._effect = HandlerLlmOpenaiCompatible(transport=transport)
        self._registry = _load_registry()

    async def handle(self, command: ModelAbCompareStart) -> ModelAbCompareResult:
        system_prompt = command.system_prompt or _DEFAULT_SYSTEM_PROMPT

        resolved, skipped = _resolve_models(self._registry, command.models)

        if not resolved:
            return ModelAbCompareResult(
                comparison=[],
                correlation_id=command.correlation_id,
                status="PARTIAL",
                models_skipped=skipped,
            )

        # Fan out inference in parallel — the effect handler handles connection
        # errors gracefully; we catch exceptions and record them in the row.
        inference_results = await asyncio.gather(
            *(
                self._call_model(m, system_prompt, command.task, command.correlation_id)
                for m in resolved
            ),
            return_exceptions=True,
        )

        rows: list[ModelComparisonRow] = []
        for model, result in zip(resolved, inference_results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("Model %s raised: %s", model.model_id, result)
                rows.append(
                    ModelComparisonRow(
                        model_key=model.model_id,
                        display_name=model.display_name,
                        error=str(result),
                    )
                )
                continue

            # result is a ModelLlmInferenceResponse
            prompt_tokens = result.usage.tokens_input
            completion_tokens = result.usage.tokens_output
            total_tokens = result.usage.tokens_total
            latency_ms = int(result.latency_ms)
            generated_text = result.generated_text or ""

            cost = _calculate_cost(model, prompt_tokens, completion_tokens)
            quality = ""
            if command.quality_check and generated_text:
                quality = _run_quality_check(generated_text)

            rows.append(
                ModelComparisonRow(
                    model_key=model.model_id,
                    display_name=model.display_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    quality=quality,
                    error="",
                )
            )

        rows.sort(key=lambda r: r.cost_usd)
        status = "COMPLETED" if not skipped else "PARTIAL"
        return ModelAbCompareResult(
            comparison=rows,
            correlation_id=command.correlation_id,
            status=status,
            models_skipped=skipped,
        )

    async def _call_model(
        self,
        model: _ResolvedModel,
        system_prompt: str,
        user_prompt: str,
        correlation_id: str,
    ) -> Any:
        from omnibase_infra.enums import EnumLlmOperationType
        from omnibase_infra.nodes.node_llm_inference_effect.models.model_llm_inference_request import (
            ModelLlmInferenceRequest,
        )

        request = ModelLlmInferenceRequest(
            base_url=model.endpoint_url,
            operation_type=EnumLlmOperationType.CHAT_COMPLETION,
            model=model.model_id_resolved,
            messages=({"role": "user", "content": user_prompt},),
            system_prompt=system_prompt,
            api_key=model.api_key,
            timeout_seconds=120.0,
        )
        return await self._effect.handle(request)


__all__: list[str] = [
    "HandlerAbCompareOrchestrator",
]
