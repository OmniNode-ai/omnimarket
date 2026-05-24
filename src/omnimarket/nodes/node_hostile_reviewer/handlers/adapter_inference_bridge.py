"""Inference Bridge Adapter — bridges orchestrator to node_llm_inference_effect.

Resolves model configuration, constructs OpenAI-compatible requests, dispatches
via HTTP or CLI subprocess, and returns raw response text.

Defines ``ModelInferenceAdapter`` ABC consumed by the review orchestrator.

Use ``build_from_contract()`` to construct an adapter from caller-provided
logical route keys and contract-declared model_routing policy schema.
"""

from __future__ import annotations

import logging
import os
import subprocess
from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Final

import httpx
from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

_RESERVED_EXTRA_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    {"authorization", "content-type"}
)


class ModelInferenceAdapter(ABC):
    """Protocol for dispatching inference to node_llm_inference_effect."""

    @abstractmethod
    async def infer(
        self,
        model_key: str,
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None = None,
    ) -> str:
        """Send prompt to a model and return raw response text."""
        ...


class ModelInferenceBridgeConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    model_configs: dict[str, dict[str, object]] = Field(
        default_factory=dict,
        description="Per-logical-route config: base_url, model_id, transport, context_window, timeout_seconds",
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
        temperature: float | None = None,
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
            model_key,
            model_cfg,
            system_prompt,
            user_prompt,
            timeout_seconds,
            temperature,
        )

    async def _call_http_model(
        self,
        model_key: str,
        cfg: dict[str, object],
        system_prompt: str,
        user_prompt: str,
        timeout_seconds: float,
        temperature: float | None,
    ) -> str:
        base_url = str(cfg.get("base_url", ""))
        if not base_url:
            base_url_env = str(cfg.get("base_url_env", ""))
            base_url = os.environ.get(base_url_env, "")
        if not base_url:
            msg = f"Model route {model_key!r} is missing base_url"
            raise ValueError(msg)

        model_id = str(cfg.get("model_id", ""))
        if not model_id:
            msg = f"Model route {model_key!r} is missing model_id"
            raise ValueError(msg)

        api_key = str(cfg.get("api_key", "")) or None

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        raw_temperature = (
            temperature if temperature is not None else cfg.get("temperature", 0.2)
        )
        if not isinstance(raw_temperature, (int, float, str)):
            raw_temperature = 0.2

        extra_headers = cfg.get("extra_headers")
        if isinstance(extra_headers, dict):
            for k, v in extra_headers.items():
                header_name = str(k)
                if header_name.lower() in _RESERVED_EXTRA_HEADER_NAMES:
                    logger.warning("Ignoring reserved extra header: %s", header_name)
                    continue
                headers[header_name] = str(v)

        payload = {
            "model": model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": 2048,
            "temperature": float(raw_temperature),
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
        cli_command = str(cfg.get("cli_command", ""))
        if not cli_command:
            msg = f"Model route {model_key!r} is missing cli_command"
            raise ValueError(msg)
        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
        result = subprocess.run(
            [cli_command, combined_prompt],
            capture_output=True,
            text=True,
            timeout=int(timeout_seconds),
            check=False,
        )
        return result.stdout.strip()


def build_from_contract(
    requested_keys: list[str] | None = None,
    runtime_model_configs: Mapping[str, Mapping[str, object]] | None = None,
) -> AdapterInferenceBridge:
    """Build an AdapterInferenceBridge from logical route keys and runtime configs.

    The node contract owns the policy schema. Concrete route configs must be
    supplied by the caller or via the contract-declared JSON env var. Missing
    requested keys or incomplete route configs raise ValueError.

    Args:
        requested_keys: Required logical route keys to load.
        runtime_model_configs: Optional runtime configs keyed by logical route key.

    Returns:
        AdapterInferenceBridge wired with requested model configs.
    """
    from omnimarket.nodes.node_hostile_reviewer.handlers.model_config_loader import (
        build_model_configs,
    )

    configs = build_model_configs(
        requested_keys=requested_keys,
        runtime_model_configs=runtime_model_configs,
    )
    return AdapterInferenceBridge(ModelInferenceBridgeConfig(model_configs=configs))


__all__: list[str] = [
    "AdapterInferenceBridge",
    "ModelInferenceAdapter",
    "ModelInferenceBridgeConfig",
    "build_from_contract",
]
