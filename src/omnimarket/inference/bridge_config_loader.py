# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build ``ModelInferenceBridgeConfig`` from ``LLM_*_URL`` env vars.

``ModelInferenceBridgeConfig.model_configs`` stores per-reviewer-key endpoint
metadata (base_url, model_id, transport, context_window). Historically this
dict defaulted to empty and every reviewer key failed with
``ValueError: Unknown model_key`` (OMN-9351 Bug 1).

This loader is the single source of truth for mapping canonical short keys
(``qwen3-coder``, ``qwen3-14b``, ``deepseek-r1``, ``qwen3-next``, ``glm``)
onto the corresponding ``LLM_*_URL`` endpoint so nodes no longer duplicate
the wiring inline.

Missing env vars simply omit the key — the loader never raises. That lets
callers pass whatever subset of keys is actually configured on the current
host without a startup-time health probe.

The canonical short keys are intentionally aligned with
``aggregate_reviews.py`` in the hostile_reviewer skill (that CLI script
already drives ``LLM_CODER_URL``/``LLM_DEEPSEEK_R1_URL`` for the same
purpose). Keep this table and that script in sync if either side grows a
new model.
"""

from __future__ import annotations

import os
from typing import Final

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceBridgeConfig,
)

# key -> (url env var, model_id env var, default model_id, context window)
_MODEL_KEY_REGISTRY: Final[tuple[tuple[str, str, str, str, int], ...]] = (
    (
        "qwen3-coder",
        "LLM_CODER_URL",
        "LLM_CODER_MODEL_NAME",
        "cyankiwi/Qwen3-Coder-30B-A3B-Instruct-AWQ-4bit",
        112_000,
    ),
    (
        "qwen3-14b",
        "LLM_CODER_FAST_URL",
        "LLM_CODER_FAST_MODEL_NAME",
        "Qwen/Qwen3-14B-AWQ",
        24_000,
    ),
    (
        "deepseek-r1",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_DEEPSEEK_R1_MODEL_NAME",
        "mlx-community/DeepSeek-R1-Distill-Qwen-32B-bf16",
        8_192,
    ),
    (
        "qwen3-next",
        "LLM_QWEN3_NEXT_URL",
        "LLM_QWEN3_NEXT_MODEL_NAME",
        "mlx-community/Qwen3-Next-80B-A3B-Instruct-4bit",
        8_192,
    ),
    (
        "glm",
        "LLM_GLM_URL",
        "LLM_GLM_MODEL_NAME",
        "glm-4.5",
        128_000,
    ),
)

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0


def load_inference_bridge_config_from_env() -> ModelInferenceBridgeConfig:
    """Return a ``ModelInferenceBridgeConfig`` populated from env vars.

    For each registry entry: if the URL env var is set, register the key
    with ``base_url``, ``model_id`` (from the model-name env var or default),
    ``transport="http"``, ``context_window``, and ``timeout_seconds``.
    GLM also picks up ``api_key`` from ``LLM_GLM_API_KEY`` when present.
    """
    model_configs: dict[str, dict[str, object]] = {}

    for (
        key,
        url_env,
        model_env,
        default_model_id,
        context_window,
    ) in _MODEL_KEY_REGISTRY:
        base_url = os.environ.get(url_env, "").strip()
        if not base_url:
            continue

        cfg: dict[str, object] = {
            "base_url": base_url,
            "model_id": os.environ.get(model_env, default_model_id),
            "transport": "http",
            "context_window": context_window,
            "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
        }

        if key == "glm":
            api_key = os.environ.get("LLM_GLM_API_KEY", "").strip()
            if api_key:
                cfg["api_key"] = api_key

        model_configs[key] = cfg

    return ModelInferenceBridgeConfig(model_configs=model_configs)


__all__: list[str] = ["load_inference_bridge_config_from_env"]
