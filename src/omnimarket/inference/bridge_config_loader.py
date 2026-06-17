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
is present. The base URL is resolved from ``OPENROUTER_BASE_URL`` config —
there is NO hardcoded in-code provider URL default (OMN-12824). When OpenRouter
is configured (key present) but the base URL is missing, registration fails
closed rather than substituting a baked-in literal.
"""

from __future__ import annotations

import os
from typing import Final

from pydantic import SecretStr

from omnimarket.inference.adapter_inference_bridge import (
    ModelInferenceBridgeConfig,
)
from omnimarket.inference.openrouter_models import (
    EnumModelAvailability,
    get_openrouter_models,
)
from omnimarket.inference.registry_context_windows import (
    get_context_window_for_endpoint_env,
)
from omnimarket.inference.secret_store_resolver import (
    resolve_api_key,
    resolve_api_key_async,
)

# key -> (url env var, model_id env var)
# Context windows are resolved from the model registry at load time via
# get_context_window_for_endpoint_env.  The fallback dict below covers
# endpoint env vars not yet in model_registry_v1.yaml (e.g. frontier cloud).
# model_id is always resolved from the env var — no hardcoded defaults.
# If the model-name env var is unset, model_id resolves to "" and the
# downstream API call will fail immediately with an invalid-model error.
_MODEL_KEY_REGISTRY: Final[tuple[tuple[str, str, str], ...]] = (
    ("qwen3-coder", "LLM_CODER_URL", "LLM_CODER_MODEL_NAME"),
    ("qwen3-14b", "LLM_CODER_FAST_URL", "LLM_CODER_FAST_MODEL_NAME"),
    ("deepseek-r1", "LLM_DEEPSEEK_R1_URL", "LLM_DEEPSEEK_R1_MODEL_NAME"),
    ("qwen3-next", "LLM_QWEN3_NEXT_URL", "LLM_QWEN3_NEXT_MODEL_NAME"),
    ("glm", "LLM_GLM_URL", "LLM_GLM_MODEL_NAME"),
)

# Fallback context windows for endpoint env vars that have no registry entry.
# Add entries here only when the provider is not yet in model_registry_v1.yaml.
# Remove entries when the provider graduates to the registry.
_CONTEXT_WINDOW_FALLBACKS: Final[dict[str, int]] = {
    "LLM_GLM_URL": 128_000,
}

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0


def load_inference_bridge_config_from_env() -> ModelInferenceBridgeConfig:
    """Return a ``ModelInferenceBridgeConfig`` populated from env vars.

    Sync entry point. The OpenRouter key is resolved via the sync
    ``resolve_api_key`` and therefore MUST NOT be called from inside a running
    event loop — the sync resolver fails closed there. Async callers (and the
    runtime auto-wiring boot path) must use
    :func:`load_inference_bridge_config_from_env_async` instead.

    For each registry entry: if the URL env var is set, register the key
    with ``base_url``, ``model_id`` (from the model-name env var, empty string
    if unset), ``transport="http"``, ``context_window``, and ``timeout_seconds``.
    GLM also picks up ``api_key`` from ``LLM_GLM_API_KEY`` when present.

    OpenRouter models are registered as ``openrouter/<model_id>`` keys when
    OPENROUTER_API_KEY is set. Each entry carries the OpenRouter base URL,
    model_id, and required HTTP-Referer / X-Title headers.
    """
    model_configs = _build_static_model_configs()
    openrouter_key = resolve_api_key("OPENROUTER_API_KEY", required=False)
    _register_openrouter_models(model_configs, openrouter_key)
    return ModelInferenceBridgeConfig(model_configs=model_configs)


async def load_inference_bridge_config_from_env_async() -> ModelInferenceBridgeConfig:
    """Async variant of :func:`load_inference_bridge_config_from_env`.

    Identical wiring, but resolves the OpenRouter key through
    ``resolve_api_key_async`` so it is safe to call from inside a running event
    loop (the runtime auto-wiring boot path, ``HandlerSegmentation.handle``).
    The static ``LLM_*`` env reads are pure and shared with the sync loader.
    """
    model_configs = _build_static_model_configs()
    openrouter_key = await resolve_api_key_async("OPENROUTER_API_KEY", required=False)
    _register_openrouter_models(model_configs, openrouter_key)
    return ModelInferenceBridgeConfig(model_configs=model_configs)


def _build_static_model_configs() -> dict[str, dict[str, object]]:
    """Build the env-driven static model configs (no secret-store resolution).

    Pure with respect to the secret store — only reads ``LLM_*`` env vars — so
    it is shared verbatim by the sync and async loaders.
    """
    model_configs: dict[str, dict[str, object]] = {}

    for key, url_env, model_env in _MODEL_KEY_REGISTRY:
        base_url = os.environ.get(url_env, "").strip()
        if not base_url:
            continue

        context_window = get_context_window_for_endpoint_env(
            url_env,
            fallback=_CONTEXT_WINDOW_FALLBACKS.get(url_env, 32_000),
        )
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

    return model_configs


def _register_openrouter_models(
    model_configs: dict[str, dict[str, object]],
    openrouter_key: SecretStr | None,
) -> None:
    """Populate model_configs with OpenRouter free-tier entries.

    ``openrouter_key`` is the already-resolved secret (sync or async). Skips
    silently when it is ``None`` (or empty) so callers never fail on hosts that
    don't have OpenRouter configured. When the key IS present but
    ``OPENROUTER_BASE_URL`` is missing, registration fails closed (OMN-12824):
    there is no hardcoded in-code provider URL default to fall back to.
    """
    if openrouter_key is None:
        return
    api_key = openrouter_key.get_secret_value().strip()
    if not api_key:
        return

    base_url = os.environ.get(
        "OPENROUTER_BASE_URL", ""
    ).strip()  # contract-config-ok: config
    if not base_url:
        raise ValueError(
            "OpenRouter is configured (OPENROUTER_API_KEY present) but "
            "OPENROUTER_BASE_URL is missing. Declare the OpenRouter base URL in "
            "config — no hardcoded provider URL default is permitted (OMN-12824)."
        )

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


__all__: list[str] = [
    "load_inference_bridge_config_from_env",
    "load_inference_bridge_config_from_env_async",
]
