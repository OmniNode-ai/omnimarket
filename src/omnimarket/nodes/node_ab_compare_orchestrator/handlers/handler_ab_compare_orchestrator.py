# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAbCompareOrchestrator — fans out inference to N models in parallel.

Reads models_registry.yaml at construction time. At handle() time:
  1. Resolves env vars; skips models with missing required API key.
  2. Fans out asyncio.gather() calls to the injected effect handler.
  3. Calculates cost from registry pricing fields.
  4. Returns ModelAbCompareResult with comparison rows sorted by cost.

In RuntimeLocal mode, inject HandlerAbInferenceEffect directly.
The injected ProtocolInferenceEffect is the only I/O surface — tests inject a fake.
No HTTP calls, no health probes, no httpx imports — all I/O is delegated to the
effect node which handles connection errors gracefully and returns error fields.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

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


# ---------------------------------------------------------------------------
# Protocol — matches HandlerAbInferenceEffect.handle() signature exactly.
# In production: inject HandlerAbInferenceEffect directly.
# In tests: inject a fake that satisfies this protocol.
# ---------------------------------------------------------------------------


class _InferenceRequest(Protocol):
    """Minimal view of ModelInferenceRequest needed by the protocol."""

    model_key: str
    endpoint_url: str
    model_id: str
    protocol: str
    prompt: str
    system_prompt: str
    correlation_id: str
    timeout_seconds: float


class _InferenceResult(Protocol):
    """Minimal view of ModelInferenceResult needed by the protocol."""

    model_key: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    latency_ms: int
    raw_output: str
    error: str
    correlation_id: str


class ProtocolInferenceEffect(Protocol):
    """Injectable effect handler — real impl is HandlerAbInferenceEffect, tests use a fake."""

    async def handle(self, request: Any) -> Any:
        """Call the effect with a ModelInferenceRequest, return ModelInferenceResult."""
        ...


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
        if requires_key and not os.environ.get(requires_key):
            logger.info("Skipping %s: missing env var %s", model_id, requires_key)
            skipped.append(model_id)
            continue

        # Endpoint resolution — direct base URL from registry; effect node appends path.
        if entry.get("protocol") == "anthropic":
            endpoint_url = "anthropic_sdk"
        else:
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
                protocol=entry["protocol"],
                model_id_resolved=model_id_resolved,
                cost_per_1k_input=float(entry["cost_per_1k_input"]),
                cost_per_1k_output=float(entry["cost_per_1k_output"]),
                location=entry["location"],
                context_window=int(entry["context_window"]),
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


def _make_inference_request(
    model: _ResolvedModel,
    system_prompt: str,
    user_prompt: str,
    correlation_id: str,
    timeout_seconds: float = 120.0,
) -> Any:
    """Build a ModelInferenceRequest dict (used when the effect type isn't imported)."""
    # Import lazily so the orchestrator doesn't require the effect package at import time.
    # When the effect PR is merged, this will resolve from the installed package.
    try:
        from omnimarket.nodes.node_ab_inference_effect.models.model_inference_request import (
            ModelInferenceRequest,
        )

        return ModelInferenceRequest(
            model_key=model.model_id,
            endpoint_url=model.endpoint_url,
            model_id=model.model_id_resolved,
            protocol=model.protocol,
            prompt=user_prompt,
            system_prompt=system_prompt,
            correlation_id=correlation_id,
            timeout_seconds=timeout_seconds,
        )
    except ImportError:
        # Fallback: return a plain object that satisfies the protocol via __dict__
        # This path is only hit in tests that inject a fake effect handler.
        return _SimpleRequest(
            model_key=model.model_id,
            endpoint_url=model.endpoint_url,
            model_id=model.model_id_resolved,
            protocol=model.protocol,
            prompt=user_prompt,
            system_prompt=system_prompt,
            correlation_id=correlation_id,
            timeout_seconds=timeout_seconds,
        )


class _SimpleRequest(BaseModel):
    """Fallback request object used when node_ab_inference_effect is not installed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_key: str
    endpoint_url: str
    model_id: str
    protocol: str
    prompt: str
    system_prompt: str
    correlation_id: str
    timeout_seconds: float = 120.0


class HandlerAbCompareOrchestrator:
    """Orchestrator: loads registry, fans out to effect node, collects, emits."""

    def __init__(self, effect_handler: ProtocolInferenceEffect | None = None) -> None:
        if effect_handler is not None:
            self._effect = effect_handler
        else:
            # Production: import and instantiate the real effect handler.
            from omnimarket.nodes.node_ab_inference_effect.handlers.handler_ab_inference_effect import (
                HandlerAbInferenceEffect,
            )

            self._effect = HandlerAbInferenceEffect()
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
        # errors gracefully and returns a result with a non-empty error field.
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

            # result is a ModelInferenceResult (or protocol-compatible object)
            if result.error:
                logger.warning(
                    "Model %s returned error: %s", model.model_id, result.error
                )

            cost = _calculate_cost(
                model, result.prompt_tokens, result.completion_tokens
            )
            quality = ""
            if command.quality_check and result.raw_output and not result.error:
                quality = _run_quality_check(result.raw_output)

            rows.append(
                ModelComparisonRow(
                    model_key=model.model_id,
                    display_name=model.display_name,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                    total_tokens=result.total_tokens,
                    cost_usd=cost,
                    latency_ms=result.latency_ms,
                    quality=quality,
                    error=result.error,
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
        request = _make_inference_request(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            correlation_id=correlation_id,
        )
        return await self._effect.handle(request)


__all__: list[str] = [
    "HandlerAbCompareOrchestrator",
    "ProtocolInferenceEffect",
]
