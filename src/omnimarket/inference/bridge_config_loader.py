# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build ``ModelInferenceBridgeConfig`` from Settings or ``LLM_*_URL`` env vars.

``ModelInferenceBridgeConfig.model_configs`` stores per-reviewer-key endpoint
metadata (base_url, model_id, transport, context_window). Historically this
dict defaulted to empty and every reviewer key failed with
``ValueError: Unknown model_key`` (OMN-9351 Bug 1).

This loader is the single source of truth for mapping canonical short keys
(``qwen3-coder``, ``qwen3-14b``, ``deepseek-r1``, ``qwen3-next``, ``glm``)
onto the corresponding ``LLM_*_URL`` endpoint so nodes no longer duplicate
the wiring inline.

Missing env vars / settings fields simply omit the key — the loader never
raises for missing config. Callers can pass whatever subset of keys is
actually configured on the current host without a startup-time health probe.

The canonical short keys are intentionally aligned with
``aggregate_reviews.py`` in the hostile_reviewer skill (that CLI script
already drives ``LLM_CODER_URL``/``LLM_DEEPSEEK_R1_URL`` for the same
purpose). Keep this table and that script in sync if either side grows a
new model.

Raises ``ConfigError`` only when explicit validation is requested via
``load_inference_bridge_config(settings, validate_keys=[...])``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    ModelInferenceBridgeConfig,
)

if TYPE_CHECKING:
    from omnimarket.config.settings import Settings

# ---------------------------------------------------------------------------
# Internal registry
# ---------------------------------------------------------------------------

# Each tuple: (short_key, url_settings_attr, model_id_settings_attr,
#              url_env_var, model_id_env_var, context_window)
#
# url_settings_attr / model_id_settings_attr name Settings fields (snake_case).
# url_env_var / model_id_env_var are the raw env var names used by the
# env-based fallback path. Fields absent from the Settings class return ""
# via getattr default — those keys are silently skipped by load_inference_bridge_config.
_MODEL_KEY_REGISTRY: Final[tuple[tuple[str, str, str, str, str, int], ...]] = (
    (
        "qwen3-coder",
        "llm_coder_url",
        "llm_coder_model_id",
        "LLM_CODER_URL",
        "LLM_CODER_MODEL_NAME",
        112_000,
    ),
    (
        "qwen3-14b",
        "llm_coder_fast_url",
        "llm_coder_fast_model_id",
        "LLM_CODER_FAST_URL",
        "LLM_CODER_FAST_MODEL_NAME",
        24_000,
    ),
    (
        "deepseek-r1",
        "llm_reasoner_url",
        "llm_reasoner_model_id",
        "LLM_DEEPSEEK_R1_URL",
        "LLM_DEEPSEEK_R1_MODEL_NAME",
        8_192,
    ),
    (
        "qwen3-next",
        "llm_qwen3_next_url",
        "llm_qwen3_next_model_id",
        "LLM_QWEN3_NEXT_URL",
        "LLM_QWEN3_NEXT_MODEL_NAME",
        8_192,
    ),
    (
        "glm",
        "llm_glm_url",
        "llm_glm_model_name",
        "LLM_GLM_URL",
        "LLM_GLM_MODEL_NAME",
        128_000,
    ),
)

_DEFAULT_TIMEOUT_SECONDS: Final[float] = 120.0


class ConfigError(ValueError):
    """Raised when required config keys are missing during explicit validation."""

    def __init__(self, missing_keys: list[str]) -> None:
        self.missing_keys = missing_keys
        super().__init__(
            f"Missing required inference config keys: {', '.join(missing_keys)}"
        )


# ---------------------------------------------------------------------------
# Settings-based loader (primary path)
# ---------------------------------------------------------------------------


def load_inference_bridge_config(
    settings: Settings,
    *,
    validate_keys: list[str] | None = None,
) -> ModelInferenceBridgeConfig:
    """Return a ``ModelInferenceBridgeConfig`` populated from ``settings``.

    For each registry entry: if the URL field is non-empty, register the key
    with ``base_url``, ``model_id`` (from settings), ``transport="http"``,
    ``context_window``, and ``timeout_seconds``.
    GLM also picks up ``api_key`` from ``llm_glm_api_key`` when present.

    When ``validate_keys`` is provided, raises ``ConfigError`` naming any
    keys whose URL field is empty (i.e., the model is not configured).
    """
    model_configs: dict[str, dict[str, object]] = {}

    for (
        key,
        url_attr,
        model_id_attr,
        _url_env,
        _model_env,
        context_window,
    ) in _MODEL_KEY_REGISTRY:
        base_url = (getattr(settings, url_attr, "") or "").strip()
        if not base_url:
            continue

        model_id = (getattr(settings, model_id_attr, "") or "").strip()
        if not model_id:
            continue

        cfg: dict[str, object] = {
            "base_url": base_url,
            "model_id": model_id,
            "transport": "http",
            "context_window": context_window,
            "timeout_seconds": _DEFAULT_TIMEOUT_SECONDS,
        }

        if key == "glm":
            api_key = settings.llm_glm_api_key.get_secret_value().strip()
            if api_key:
                cfg["api_key"] = api_key

        model_configs[key] = cfg

    if validate_keys:
        missing = [k for k in validate_keys if k not in model_configs]
        if missing:
            raise ConfigError(missing)

    return ModelInferenceBridgeConfig(model_configs=model_configs)


# ---------------------------------------------------------------------------
# Env-var fallback loader (backwards-compat for callers without Settings)
# ---------------------------------------------------------------------------


def load_inference_bridge_config_from_env() -> ModelInferenceBridgeConfig:
    """Return a ``ModelInferenceBridgeConfig`` populated from env vars.

    Delegates to ``load_inference_bridge_config`` after constructing a
    minimal Settings-compatible namespace from env vars. Prefer
    ``load_inference_bridge_config(settings)`` in new code.
    """
    model_configs: dict[str, dict[str, object]] = {}

    for (
        key,
        _url_attr,
        _model_id_attr,
        url_env,
        model_env,
        context_window,
    ) in _MODEL_KEY_REGISTRY:
        base_url = os.environ.get(url_env, "").strip()
        if not base_url:
            continue

        model_id = os.environ.get(model_env, "").strip()
        if not model_id:
            continue

        cfg: dict[str, object] = {
            "base_url": base_url,
            "model_id": model_id,
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


__all__: list[str] = [
    "ConfigError",
    "load_inference_bridge_config",
    "load_inference_bridge_config_from_env",
]
