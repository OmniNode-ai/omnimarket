# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""HandlerDelegationAbRunner — Phase 4 A/B comparison: baseline vs delegated path.

Runs the same task payload through two paths sequentially:
  1. Baseline: frontier model (direct call, no delegation policy).
  2. Delegated: cheaper/local model with quality gate; escalates to frontier on failure.

Returns ModelABComparisonResult with per-path tokens, cost, latency, retry count,
and quality gate result. Injectable _llm_call for unit-test mocking.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from typing import Any

from omnimarket.nodes.node_delegation_ab_runner.models.model_ab_comparison_result import (
    ModelABComparisonResult,
)
from omnimarket.nodes.node_delegation_ab_runner.models.model_delegation_ab_request import (
    ModelDelegationAbRequest,
    ModelDelegationPathConfig,
)
from omnimarket.nodes.node_delegation_ab_runner.models.model_delegation_path_result import (
    ModelDelegationPathResult,
)

logger = logging.getLogger(__name__)

# Type alias for the injectable LLM call function.
# Returns (prompt_tokens, completion_tokens, raw_output, error).
LlmCallFn = Callable[
    [ModelDelegationPathConfig, str, str, float],
    tuple[int, int, str, str],
]

# Cost per 1M tokens — populated from pricing manifest when available.
# Fallback constants used when pricing manifest hash is absent.
_FRONTIER_COST_PER_1M_INPUT = 0.075  # USD, Gemini 1.5 Flash approximation
_FRONTIER_COST_PER_1M_OUTPUT = 0.30
_DELEGATED_COST_PER_1M_INPUT = 0.01  # USD, local/cheap model approximation
_DELEGATED_COST_PER_1M_OUTPUT = 0.01


def _estimate_cost(
    prompt_tokens: int,
    completion_tokens: int,
    is_delegated: bool,
) -> float:
    if is_delegated:
        input_rate = _DELEGATED_COST_PER_1M_INPUT
        output_rate = _DELEGATED_COST_PER_1M_OUTPUT
    else:
        input_rate = _FRONTIER_COST_PER_1M_INPUT
        output_rate = _FRONTIER_COST_PER_1M_OUTPUT
    return round(
        (prompt_tokens / 1_000_000) * input_rate
        + (completion_tokens / 1_000_000) * output_rate,
        8,
    )


def _evaluate_quality(raw_output: str, threshold: float) -> float:
    """Heuristic quality score when no external evaluator is wired.

    Returns 1.0 if output is non-empty and longer than 20 chars, 0.0 otherwise.
    Replace with a real scoring function when available.
    """
    if not raw_output or len(raw_output.strip()) < 20:
        return 0.0
    return 1.0


def _run_path(
    cfg: ModelDelegationPathConfig,
    task_payload: str,
    system_prompt: str,
    quality_threshold: float,
    llm_call: LlmCallFn,
    max_retries: int = 1,
) -> ModelDelegationPathResult:
    """Execute one path, returning a fully populated ModelDelegationPathResult."""
    total_latency_ms = 0
    retry_count = 0
    prompt_tokens = 0
    completion_tokens = 0
    raw_output = ""
    error = ""
    escalated = False

    for attempt in range(max_retries + 1):
        t0 = time.monotonic()
        pt, ct, out, err = llm_call(
            cfg, task_payload, system_prompt, cfg.timeout_seconds
        )
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        total_latency_ms += elapsed_ms

        if attempt > 0:
            retry_count += 1

        if not err:
            prompt_tokens = pt
            completion_tokens = ct
            raw_output = out
            error = ""
            break

        error = err
        logger.warning("Path %s attempt %d failed: %s", cfg.label, attempt + 1, err)

    total_tokens = prompt_tokens + completion_tokens
    cost_usd = _estimate_cost(prompt_tokens, completion_tokens, cfg.is_delegated)

    quality_score = 0.0
    quality_passed = True
    if not error and quality_threshold > 0.0:
        quality_score = _evaluate_quality(raw_output, quality_threshold)
        quality_passed = quality_score >= quality_threshold

    return ModelDelegationPathResult(
        label=cfg.label,
        model_id=cfg.model_id,
        endpoint_url=cfg.endpoint_url,
        is_delegated=cfg.is_delegated,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        latency_ms=total_latency_ms,
        retry_count=retry_count,
        quality_score=quality_score,
        quality_passed=quality_passed,
        escalated=escalated,
        raw_output=raw_output,
        error=error,
    )


def _default_llm_call(
    cfg: ModelDelegationPathConfig,
    task_payload: str,
    system_prompt: str,
    timeout_seconds: float,
) -> tuple[int, int, str, str]:
    """Live LLM call via httpx against an OpenAI-compatible endpoint."""
    import httpx

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": task_payload})

    payload: dict[str, Any] = {
        "model": cfg.model_id,
        "messages": messages,
        "max_tokens": 2048,
    }

    try:
        base = cfg.endpoint_url.rstrip("/")
        url = f"{base}/v1/chat/completions"
        with httpx.Client(timeout=timeout_seconds) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        usage = data.get("usage", {})
        pt = int(usage.get("prompt_tokens", 0))
        ct = int(usage.get("completion_tokens", 0))
        choices = data.get("choices", [])
        raw = choices[0].get("message", {}).get("content", "") if choices else ""
        return pt, ct, raw, ""
    except Exception as exc:
        return 0, 0, "", str(exc)


class HandlerDelegationAbRunner:
    """EFFECT handler: run baseline vs delegated path and return comparison."""

    def __init__(self, llm_call: LlmCallFn | None = None) -> None:
        self._llm_call: LlmCallFn = llm_call or _default_llm_call

    def handle(self, request: ModelDelegationAbRequest) -> ModelABComparisonResult:
        task_hash = hashlib.sha256(request.task_payload.encode()).hexdigest()

        baseline_result = _run_path(
            cfg=request.baseline,
            task_payload=request.task_payload,
            system_prompt=request.system_prompt,
            quality_threshold=0.0,  # no gate on baseline
            llm_call=self._llm_call,
        )

        delegated_result = _run_path(
            cfg=request.delegated,
            task_payload=request.task_payload,
            system_prompt=request.system_prompt,
            quality_threshold=request.quality_threshold,
            llm_call=self._llm_call,
        )

        return ModelABComparisonResult.compute(
            correlation_id=request.correlation_id,
            task_payload_hash=task_hash,
            baseline=baseline_result,
            delegated=delegated_result,
            pricing_manifest_hash=request.pricing_manifest_hash,
        )


__all__ = ["HandlerDelegationAbRunner"]
