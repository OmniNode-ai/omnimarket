"""Inference Bridge Adapter — bridges orchestrator to node_llm_inference_effect.

Resolves model configuration, constructs OpenAI-compatible requests, dispatches
via HTTP or CLI subprocess, and returns raw response text.

Defines ``ModelInferenceAdapter`` ABC consumed by the review orchestrator.
"""

from __future__ import annotations

import logging
import os
import subprocess
from abc import ABC, abstractmethod

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class ModelInferenceAdapter(ABC):
    """Protocol for dispatching inference to node_llm_inference_effect."""

    @abstractmethod
    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        """Send prompt to a model and return raw response text."""
        ...


class ModelInferenceBridgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_configs: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description="Per-model config: base_url, model_id, transport, context_window, timeout_seconds",
    )


class AdapterInferenceBridge(ModelInferenceAdapter):
    """Concrete inference adapter using OpenAI-compatible HTTP or CLI subprocess."""

    def __init__(self, config: ModelInferenceBridgeConfig) -> None:
        self._config = config

    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        model_cfg = self._config.model_configs.get(model_key)
        if model_cfg is None:
            msg = f"Unknown model_key: {model_key!r}"
            raise ValueError(msg)

        transport = str(model_cfg.get("transport", "http"))
        if transport == "cli":
            return await self._call_cli_model(
                model_key, model_cfg, system_prompt, user_prompt, timeout_seconds
            )
        return await self._call_http_model(
            model_key, model_cfg, system_prompt, user_prompt, timeout_seconds
        )

    async def _call_http_model(
        self,
        model_key: str,
        cfg: dict[str, object],
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        base_url = str(cfg.get("base_url", ""))
        if not base_url:
            base_url_env = str(cfg.get("base_url_env", ""))
            base_url = os.environ.get(base_url_env, "")
        model_id = str(cfg.get("model_id", model_key))
        api_key = str(cfg.get("api_key", "")) or None

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
            "temperature": 0.2,
        }

        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            resp = await client.post(
                f"{base_url}/v1/chat/completions",
                json=payload,
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            return str(data["choices"][0]["message"]["content"])

    async def _call_cli_model(
        self,
        model_key: str,
        cfg: dict[str, object],
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
    ) -> str:
        cli_command = str(cfg.get("cli_command", model_key))
        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
        result = subprocess.run(
            [cli_command, combined_prompt],
            capture_output=True,
            text=True,
            timeout=int(timeout_seconds),
            check=False,
        )
        return result.stdout.strip()


__all__: list[str] = [
    "AdapterInferenceBridge",
    "ModelInferenceAdapter",
    "ModelInferenceBridgeConfig",
]
