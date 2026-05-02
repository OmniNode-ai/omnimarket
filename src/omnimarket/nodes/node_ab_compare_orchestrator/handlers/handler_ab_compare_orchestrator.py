# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerAbCompareOrchestrator — fans out inference to N models in parallel.

Reads models_registry.yaml at construction time. At handle() time:
  1. Resolves env vars; skips models with missing endpoint or required key.
  2. Probes health of local models (2s timeout); skips unreachable.
  3. Fans out asyncio.gather() calls to the injected LLM client.
  4. Calculates cost from registry pricing fields.
  5. Returns ModelAbCompareResult with comparison rows sorted by cost.

The injected ProtocolAbLlmClient is the only I/O surface — tests inject a fake.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol

import httpx
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
_HEALTH_PROBE_TIMEOUT = 2.0
_DEFAULT_SYSTEM_PROMPT = (
    "You are a expert software engineer. Respond with clean, working Python code only."
)


class ProtocolAbLlmClient(Protocol):
    """Injectable LLM client — real impl uses httpx/anthropic, tests use a fake."""

    async def call(
        self,
        *,
        endpoint_url: str,
        model_id: str,
        protocol: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        api_key: str | None,
    ) -> tuple[str, int, int, int]:
        """Return (raw_output, prompt_tokens, completion_tokens, latency_ms)."""
        ...


class _ResolvedModel(BaseModel):
    """A registry entry after env var resolution — ready to call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_id: str
    display_name: str
    endpoint_url: str
    endpoint_path: str
    health_path: str
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
        if requires_key and not os.environ.get(requires_key):
            logger.info("Skipping %s: missing env var %s", model_id, requires_key)
            skipped.append(model_id)
            continue

        # Endpoint resolution
        if entry.get("protocol") == "anthropic":
            endpoint_url = "anthropic_sdk"
            api_key = os.environ.get("ANTHROPIC_API_KEY")
        else:
            endpoint_env: str | None = entry.get("endpoint_env")
            if not endpoint_env:
                logger.info("Skipping %s: no endpoint_env declared", model_id)
                skipped.append(model_id)
                continue
            base = os.environ.get(endpoint_env, "").rstrip("/")
            if not base:
                logger.info("Skipping %s: env var %s is unset", model_id, endpoint_env)
                skipped.append(model_id)
                continue
            endpoint_url = base
            # Resolve api key from requires_key env if present
            api_key = os.environ.get(requires_key) if requires_key else None

        model_id_env: str | None = entry.get("model_id_env")
        model_id_resolved = (
            os.environ.get(model_id_env, entry["model_id_default"])
            if model_id_env
            else entry["model_id_default"]
        )

        resolved.append(
            _ResolvedModel(
                model_id=model_id,
                display_name=entry["display_name"],
                endpoint_url=endpoint_url,
                endpoint_path=entry.get("endpoint_path", "/v1/chat/completions"),
                health_path=entry.get("health_path", "/health") or "/health",
                protocol=entry["protocol"],
                model_id_resolved=model_id_resolved,
                cost_per_1k_input=float(entry["cost_per_1k_input"]),
                cost_per_1k_output=float(entry["cost_per_1k_output"]),
                location=entry["location"],
                context_window=int(entry["context_window"]),
                api_key=api_key,
            )
        )

    return resolved, skipped


async def _probe_health(model: _ResolvedModel) -> bool:
    """Return True if health endpoint responds 200, False otherwise."""
    if model.protocol == "anthropic" or model.location == "cloud":
        return True
    try:
        async with httpx.AsyncClient(timeout=_HEALTH_PROBE_TIMEOUT) as client:
            health_url = (
                f"{model.endpoint_url.rstrip('/')}/{model.health_path.lstrip('/')}"
            )
            resp = await client.get(health_url)
            return resp.status_code == 200
    except Exception:
        return False


def _run_quality_check(output: str) -> str:
    """Run ruff check on code output, return 'pass' or 'fail:<reason>'."""
    fname: str | None = None
    try:
        import tempfile

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
        return (
            "pass" if result.returncode == 0 else f"fail:{result.stdout[:120].strip()}"
        )
    except Exception as exc:
        return f"skip:{exc}"
    finally:
        if fname:
            Path(fname).unlink(missing_ok=True)


def _calculate_cost(
    model: _ResolvedModel, prompt_tokens: int, completion_tokens: int
) -> float:
    return (
        prompt_tokens * model.cost_per_1k_input
        + completion_tokens * model.cost_per_1k_output
    ) / 1000.0


class HandlerAbCompareOrchestrator:
    """Orchestrator: loads registry, probes health, fans out, collects, emits."""

    def __init__(self, llm_client: ProtocolAbLlmClient | None = None) -> None:
        self._llm_client = llm_client or _DefaultHttpLlmClient()
        self._registry = _load_registry()

    async def handle(self, command: ModelAbCompareStart) -> ModelAbCompareResult:
        system_prompt = command.system_prompt or _DEFAULT_SYSTEM_PROMPT

        resolved, skipped = _resolve_models(self._registry, command.models)

        # Probe health for local models; skip unreachable
        probe_results = await asyncio.gather(
            *(_probe_health(m) for m in resolved), return_exceptions=False
        )
        available: list[_ResolvedModel] = []
        for model, healthy in zip(resolved, probe_results, strict=True):
            if healthy:
                available.append(model)
            else:
                logger.info("Skipping %s: health probe failed", model.model_id)
                skipped.append(model.model_id)

        if not available:
            return ModelAbCompareResult(
                comparison=[],
                correlation_id=command.correlation_id,
                status="PARTIAL",
                models_skipped=skipped,
            )

        # Fan out inference in parallel
        inference_results = await asyncio.gather(
            *(self._call_model(m, system_prompt, command.task) for m in available),
            return_exceptions=True,
        )

        rows: list[ModelComparisonRow] = []
        for model, result in zip(available, inference_results, strict=True):
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

            raw_output, prompt_tokens, completion_tokens, latency_ms = result
            cost = _calculate_cost(model, prompt_tokens, completion_tokens)
            quality = ""
            if command.quality_check and raw_output:
                quality = _run_quality_check(raw_output)

            rows.append(
                ModelComparisonRow(
                    model_key=model.model_id,
                    display_name=model.display_name,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    quality=quality,
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
    ) -> tuple[str, int, int, int]:
        full_url = (
            model.endpoint_url
            if model.protocol == "anthropic"
            else f"{model.endpoint_url}{model.endpoint_path}"
        )
        return await self._llm_client.call(
            endpoint_url=full_url,
            model_id=model.model_id_resolved,
            protocol=model.protocol,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=120.0,
            api_key=model.api_key,
        )


class _DefaultHttpLlmClient:
    """Production LLM client — OpenAI-compatible HTTP + Anthropic SDK."""

    async def call(
        self,
        *,
        endpoint_url: str,
        model_id: str,
        protocol: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        api_key: str | None,
    ) -> tuple[str, int, int, int]:
        start = time.monotonic()
        if protocol == "anthropic":
            return await self._call_anthropic(
                model_id=model_id,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                timeout_seconds=timeout_seconds,
                api_key=api_key,
                start=start,
            )
        return await self._call_openai_compat(
            endpoint_url=endpoint_url,
            model_id=model_id,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            timeout_seconds=timeout_seconds,
            api_key=api_key,
            start=start,
        )

    async def _call_openai_compat(
        self,
        *,
        endpoint_url: str,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        api_key: str | None,
        start: float,
    ) -> tuple[str, int, int, int]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 2048,
        }

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(endpoint_url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        latency_ms = int((time.monotonic() - start) * 1000)
        usage = data.get("usage", {})
        prompt_tokens = int(usage.get("prompt_tokens", 0))
        completion_tokens = int(usage.get("completion_tokens", 0))
        raw_output = str(data["choices"][0]["message"]["content"])
        return raw_output, prompt_tokens, completion_tokens, latency_ms

    async def _call_anthropic(
        self,
        *,
        model_id: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        api_key: str | None,
        start: float,
    ) -> tuple[str, int, int, int]:
        try:
            import anthropic
        except ImportError as exc:
            msg = "anthropic SDK not installed; install with: uv add anthropic"
            raise RuntimeError(msg) from exc

        client = anthropic.AsyncAnthropic(
            api_key=api_key,
            timeout=timeout_seconds,
        )
        message = await client.messages.create(
            model=model_id,
            max_tokens=2048,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        latency_ms = int((time.monotonic() - start) * 1000)
        prompt_tokens = message.usage.input_tokens
        completion_tokens = message.usage.output_tokens
        first_content_block = message.content[0] if message.content else None
        raw_output = str(getattr(first_content_block, "text", ""))
        return raw_output, prompt_tokens, completion_tokens, latency_ms


__all__: list[str] = [
    "HandlerAbCompareOrchestrator",
    "ProtocolAbLlmClient",
]
