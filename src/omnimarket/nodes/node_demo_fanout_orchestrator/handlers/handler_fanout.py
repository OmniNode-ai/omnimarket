# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for node_demo_fanout_orchestrator [OMN-12235].

ORCHESTRATOR node. Accepts a list of tasks and model configs, fans out
inference requests to each model in parallel, and collects per-model
results with token counts and latency measurements.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from omnimarket.events.demo import ModelDemoInferenceResult
from omnimarket.nodes.node_demo_fanout_orchestrator.models.model_fanout_request import (
    ModelDemoFanoutRequest,
    ModelDemoFanoutResult,
    ModelDemoModelConfig,
    ModelDemoProviderFixture,
)


class HandlerDemoFanoutOrchestrator:
    """ORCHESTRATOR — fan-out LLM inference across multiple model configs."""

    async def handle(self, request: ModelDemoFanoutRequest) -> ModelDemoFanoutResult:
        jobs = [
            self._infer_one(
                task=task,
                task_index=task_index,
                config=config,
                request=request,
            )
            for task_index, task in enumerate(request.tasks)
            for config in request.model_configs
        ]
        results = await asyncio.gather(*jobs)
        return ModelDemoFanoutResult(
            run_id=request.run_id,
            correlation_id=request.correlation_id,
            results=list(results),
        )

    async def _infer_one(
        self,
        *,
        task: str,
        task_index: int,
        config: ModelDemoModelConfig,
        request: ModelDemoFanoutRequest,
    ) -> ModelDemoInferenceResult:
        if request.dry_run or config.provider == "deterministic_fixture":
            return self._fixture_result(
                task=task,
                task_index=task_index,
                config=config,
                fixture=request.provider_fixtures.get(config.model_id),
            )

        self._preflight_live_provider(config)
        if config.provider == "claude_cli":
            return await asyncio.to_thread(
                self._run_claude_cli,
                task=task,
                task_index=task_index,
                config=config,
            )
        return await asyncio.to_thread(
            self._run_openai_compatible,
            task=task,
            task_index=task_index,
            config=config,
        )

    def _fixture_result(
        self,
        *,
        task: str,
        task_index: int,
        config: ModelDemoModelConfig,
        fixture: ModelDemoProviderFixture | None,
    ) -> ModelDemoInferenceResult:
        outputs = fixture.outputs if fixture else []
        output = (
            outputs[task_index % len(outputs)]
            if outputs
            else (
                f"[dry-run:{config.model_id}] completed task {task_index + 1}: {task}"
            )
        )
        prompt_tokens = fixture.prompt_tokens if fixture else _estimate_tokens(task)
        completion_tokens = (
            fixture.completion_tokens if fixture else _estimate_tokens(output)
        )
        latency_ms = fixture.latency_ms if fixture else 0.0
        return ModelDemoInferenceResult(
            model_id=config.model_id,
            provider=config.provider,
            task_index=task_index,
            task_text=task,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            output_text=output,
        )

    @staticmethod
    def _preflight_live_provider(config: ModelDemoModelConfig) -> None:
        if config.provider == "claude_cli":
            if shutil.which("claude") is None:
                raise RuntimeError(
                    "Live demo fan-out requires the `claude` CLI for "
                    f"{config.model_id}; rerun with dry_run=true or install Claude Code."
                )
            return

        env_var = config.api_key_env_var or _default_api_key_env_var(config)
        if not os.environ.get(env_var):
            raise RuntimeError(
                f"Live demo fan-out requires {env_var} for {config.model_id}; "
                "rerun with dry_run=true to use deterministic provider fixtures."
            )

    def _run_claude_cli(
        self,
        *,
        task: str,
        task_index: int,
        config: ModelDemoModelConfig,
    ) -> ModelDemoInferenceResult:
        started = time.perf_counter()
        completed = subprocess.run(
            ["claude", "-p", "--model", config.model_id, task],
            check=False,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )
        latency_ms = (time.perf_counter() - started) * 1000.0
        output = completed.stdout.strip()
        error = completed.stderr.strip() if completed.returncode else None
        return ModelDemoInferenceResult(
            model_id=config.model_id,
            provider=config.provider,
            task_index=task_index,
            task_text=task,
            prompt_tokens=_estimate_tokens(task),
            completion_tokens=_estimate_tokens(output),
            latency_ms=latency_ms,
            output_text=output,
            error=error,
        )

    def _run_openai_compatible(
        self,
        *,
        task: str,
        task_index: int,
        config: ModelDemoModelConfig,
    ) -> ModelDemoInferenceResult:
        env_var = config.api_key_env_var or _default_api_key_env_var(config)
        endpoint = config.endpoint_url.rstrip("/") + "/chat/completions"
        body = {
            "model": config.model_id,
            "messages": [{"role": "user", "content": task}],
            "max_tokens": config.max_tokens,
            "temperature": config.temperature,
        }
        started = time.perf_counter()
        request = urllib.request.Request(
            endpoint,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {os.environ[env_var]}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=config.timeout_seconds
            ) as response:
                raw = json.loads(response.read().decode("utf-8"))
            error = None
        except urllib.error.URLError as exc:
            raw = {}
            error = str(exc)
        latency_ms = (time.perf_counter() - started) * 1000.0
        output = _extract_openai_text(raw)
        usage = raw.get("usage") if isinstance(raw, dict) else {}
        return ModelDemoInferenceResult(
            model_id=config.model_id,
            provider=config.provider,
            task_index=task_index,
            task_text=task,
            prompt_tokens=int(_field_or_estimate(usage, "prompt_tokens", task)),
            completion_tokens=int(
                _field_or_estimate(usage, "completion_tokens", output)
            ),
            latency_ms=latency_ms,
            output_text=output,
            error=error,
        )


def _default_api_key_env_var(config: ModelDemoModelConfig) -> str:
    haystack = f"{config.model_id} {config.endpoint_url}".lower()
    if "gemini" in haystack or "google" in haystack:
        return "GEMINI_API_KEY"
    return "OPENAI_API_KEY"


def _extract_openai_text(raw: dict[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _field_or_estimate(raw: object, key: str, text: str) -> int:
    if isinstance(raw, dict) and isinstance(raw.get(key), int):
        return int(raw[key])
    return _estimate_tokens(text)


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.split())) if text.strip() else 0
