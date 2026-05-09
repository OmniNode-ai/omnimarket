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

OpenRouter keys follow the pattern ``openrouter/<model_id>`` where model_id is
the full OpenRouter routing string (e.g. ``qwen/qwen3-coder:free``). These are
registered dynamically from the OpenRouter model catalog when OPENROUTER_API_KEY
is present. The base URL is declared in contract and read from
OPENROUTER_BASE_URL (defaults to https://openrouter.ai/api).
"""

from __future__ import annotations

import os
from typing import Final

from omnimarket.inference.adapter_inference_bridge import (
    ModelInferenceBridgeConfig,
)
from omnimarket.inference.openrouter_models import (
    EnumModelAvailability,
    get_openrouter_models,
)

# key -> (url env var, model_id env var, context window)
# model_id is always resolved from the env var — no hardcoded defaults.
# If the model-name env var is unset, model_id resolves to "" and the
# downstream API call will fail immediately with an invalid-model error.
_MODEL_KEY_REGISTRY: Final[tuple[tuple[str, str, str, int], ...]] = (
    ("qwen3-coder", "LLM_CODER_URL", "LLM_CODER_MODEL_NAME", 112_000),
    ("qwen3-14b", "LLM_CODER_FAST_URL", "LLM_CODER_FAST_MODEL_NAME", 24_000),
    ("deepseek-r1", "LLM_DEEPSEEK_R1_URL", "LLM_DEEPSEEK_R1_MODEL_NAME", 8_192),
    ("qwen3-next", "LLM_QWEN3_NEXT_URL", "LLM_QWEN3_NEXT_MODEL_NAME", 8_192),
    ("glm", "LLM_GLM_URL", "LLM_GLM_MODEL_NAME", 128_000),
)

# Contract-declared OpenRouter base URL. OPENROUTER_API_KEY stays in env (secret).
_OPENROUTER_BASE_URL_DEFAULT: Final[str] = "https://openrouter.ai/api"

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0


def load_inference_bridge_config_from_env() -> ModelInferenceBridgeConfig:
    """Return a ``ModelInferenceBridgeConfig`` populated from env vars.

    For each registry entry: if the URL env var is set, register the key
    with ``base_url``, ``model_id`` (from the model-name env var, empty string
    if unset), ``transport="http"``, ``context_window``, and ``timeout_seconds``.
    GLM also picks up ``api_key`` from ``LLM_GLM_API_KEY`` when present.

    OpenRouter models are registered as ``openrouter/<model_id>`` keys when
    OPENROUTER_API_KEY is set. Each entry carries the OpenRouter base URL,
    model_id, and required HTTP-Referer / X-Title headers.
    """
    model_configs: dict[str, dict[str, object]] = {}

    for key, url_env, model_env, context_window in _MODEL_KEY_REGISTRY:
        base_url = os.environ.get(url_env, "").strip()
        if not base_url:
            continue

        cfg: dict[str, object] = {
            "base_url": base_url,
            "model_id": os.environ.get(model_env, ""),
            "transport": "http",
            "context_window": context_window,
            "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
        }

        if key == "glm":
            api_key = os.environ.get("LLM_GLM_API_KEY", "").strip()
            if api_key:
                cfg["api_key"] = api_key

        model_configs[key] = cfg

    _register_openrouter_models(model_configs)

    return ModelInferenceBridgeConfig(model_configs=model_configs)


def _register_openrouter_models(model_configs: dict[str, dict[str, object]]) -> None:
    """Populate model_configs with OpenRouter free-tier entries.

    Skips silently when OPENROUTER_API_KEY is absent so callers never fail
    on hosts that don't have OpenRouter configured.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return

    raw_base_url = os.environ.get("OPENROUTER_BASE_URL")
    base_url = (raw_base_url or _OPENROUTER_BASE_URL_DEFAULT).strip()
    if not base_url:
        base_url = _OPENROUTER_BASE_URL_DEFAULT

    for model in get_openrouter_models():
        if model.availability != EnumModelAvailability.AVAILABLE:
            continue

        key = f"openrouter/{model.model_id}"
        model_configs[key] = {
            "base_url": base_url,
            "model_id": model.model_id,
            "transport": "http",
            "context_window": model.context_window,
            "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
            "api_key": api_key,
            "extra_headers": {
                "HTTP-Referer": "https://omninode.ai",
                "X-Title": "OmniNode ONEX",
            },
        }


__all__: list[str] = ["load_inference_bridge_config_from_env"]
