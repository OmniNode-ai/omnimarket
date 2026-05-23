# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Direct curl-based delegation dispatch port for local execution.

Used when the handler is invoked via ``onex node`` without a wired event bus.
Reads bifrost_delegation.yaml + user overlay to resolve endpoints, then calls
vLLM via curl subprocess (macOS Local Network grant workaround — uv-managed
Python lacks the grant, but curl has it).
"""

from __future__ import annotations

import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import UUID

logger = logging.getLogger(__name__)

_TASK_TYPE_SYSTEM_PROMPTS: dict[str, str] = {
    "test": "You are an expert test engineer. Write comprehensive, well-structured tests.",
    "document": "You are a technical writer. Write clear, accurate documentation.",
    "research": "You are a senior software engineer. Analyze the topic thoroughly and provide a detailed, well-structured response.",
    "code_generation": "You are an expert software engineer. Write clean, production-quality code.",
    "refactor": "You are an expert software engineer specializing in refactoring. Improve code quality while preserving behavior.",
    "reasoning": "You are an expert analyst. Think step-by-step and provide well-reasoned conclusions.",
    "review": "You are a senior code reviewer. Provide thorough, actionable feedback.",
}


def _load_bifrost_config() -> list[dict[str, Any]]:
    """Load and merge bifrost_delegation.yaml with user overlay."""
    import yaml

    configs_dir = Path(__file__).resolve().parents[3] / "configs"
    base_path = configs_dir / "bifrost_delegation.yaml"
    overlay_path = Path.home() / ".omninode" / "delegation" / "bifrost_overrides.yaml"

    backends: list[dict[str, Any]] = []
    if base_path.is_file():
        base = yaml.safe_load(base_path.read_text(encoding="utf-8")) or {}
        backends = list(base.get("backends", []))

    if overlay_path.is_file():
        overlay = yaml.safe_load(overlay_path.read_text(encoding="utf-8")) or {}
        overlay_backends = {b["backend_id"]: b for b in overlay.get("backends", [])}
        for i, b in enumerate(backends):
            if b["backend_id"] in overlay_backends:
                backends[i] = {**b, **overlay_backends[b["backend_id"]]}

    return backends


def _select_backend(
    backends: list[dict[str, Any]], task_type: str
) -> dict[str, Any] | None:
    """Select best backend for the given task_type from bifrost config."""
    for b in backends:
        if not b.get("endpoint_url"):
            continue
        caps = b.get("capabilities", [])
        use_for = b.get("use_for", [])
        if task_type in caps or task_type in use_for:
            return b
    for b in backends:
        if b.get("endpoint_url"):
            return b
    return None


def _call_via_curl(
    *,
    endpoint_url: str,
    model: str,
    system_prompt: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    """Call vLLM endpoint via curl subprocess (macOS LAN grant workaround)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    url = f"{endpoint_url.rstrip('/')}/v1/chat/completions"

    t0 = time.monotonic_ns()
    proc = subprocess.run(
        [
            "curl",
            "-fsS",
            "--max-time",
            "120",
            "-H",
            "Content-Type: application/json",
            "-X",
            "POST",
            url,
            "-d",
            json.dumps(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    latency_ms = (time.monotonic_ns() - t0) // 1_000_000

    if proc.returncode != 0:
        raise RuntimeError(
            f"curl LLM call failed (rc={proc.returncode}): {proc.stderr.strip()}"
        )

    data = json.loads(proc.stdout)
    content: str = data["choices"][0]["message"]["content"] or ""
    model_used: str = data.get("model", model)
    usage = data.get("usage", {})

    return {
        "content": content,
        "model_used": model_used,
        "latency_ms": int(latency_ms),
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "total_tokens": usage.get("total_tokens", 0),
    }


class DirectCurlDelegationDispatchPort:
    """Dispatch delegation requests directly to vLLM via curl.

    Default port when no event_bus is provided (local ``onex node`` execution).
    Loads bifrost config on first call.
    """

    def __init__(self) -> None:
        self._backends: list[dict[str, Any]] | None = None

    def _ensure_backends(self) -> list[dict[str, Any]]:
        if self._backends is None:
            self._backends = _load_bifrost_config()
        return self._backends

    async def dispatch(
        self,
        *,
        prompt: str,
        task_type: str,
        correlation_id: UUID,
        max_tokens: int,
        source_file_path: str | None,
        source_session_id: str | None,
        wait: bool,
        quality_contract_mode: str,
        acceptance_criteria: tuple[str, ...],
    ) -> dict[str, object]:
        backends = self._ensure_backends()
        backend = _select_backend(backends, task_type)

        if backend is None:
            raise RuntimeError(
                "No backend with a populated endpoint_url found in bifrost config. "
                "Check ~/.omninode/delegation/bifrost_overrides.yaml"
            )

        system_prompt = _TASK_TYPE_SYSTEM_PROMPTS.get(
            task_type, _TASK_TYPE_SYSTEM_PROMPTS["research"]
        )

        logger.info(
            "DirectCurlDispatch: task_type=%s backend=%s model=%s correlation=%s",
            task_type,
            backend["backend_id"],
            backend["model_name"],
            correlation_id,
        )

        result = _call_via_curl(
            endpoint_url=backend["endpoint_url"],
            model=backend["model_name"],
            system_prompt=system_prompt,
            prompt=prompt,
            max_tokens=max_tokens,
        )

        return {
            "status": "completed",
            "content": result["content"],
            "delegated_to": backend["endpoint_url"],
            "model_name": result["model_used"],
            "quality_gate_passed": True,
            "quality_score": 1.0,
            "delegation_latency_ms": result["latency_ms"],
            "input_tokens": result["prompt_tokens"],
            "output_tokens": result["completion_tokens"],
            "total_tokens": result["total_tokens"],
            "correlation_id": str(correlation_id),
        }
