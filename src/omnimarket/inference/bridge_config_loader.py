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
registered dynamically from the OpenRouter model catalog when the OpenRouter
API key resolves from the ``llm.openrouter.api_key`` secret ref through the
lane's secret store.

OMN-17372: that resolution no longer carries an ``env_var_fallback``. It
previously named ``OPEN_ROUTER_API_KEY`` literally, and ``env_var_fallback`` is
read with a direct ``os.environ.get`` INSIDE the resolver — it bypasses the
lane secret mapping entirely, so it kept working on a deployed lane even
though ``enable_convention_fallback`` is off there and the lane mapping is
otherwise the only resolution path. That made it the same house-credential
socket as the ``api_key_env`` field deleted from ``bifrost_delegation.yaml``,
just hardcoded in source instead of declared in config, and it would have
survived the config-side removal untouched. OmniNode does not offer inference:
a customer reaches OpenRouter on their OWN key, resolved per-tenant from the
managed store, so there is no house variable left for this to fall back to.
The base URL is resolved from ``OPENROUTER_BASE_URL`` config —
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
    GLM also picks up ``api_key`` when the ``llm.glm.api_key`` secret ref
    resolves through the store.

    OpenRouter models are registered as ``openrouter/<model_id>`` keys when the
    ``llm.openrouter.api_key`` secret ref resolves. Each entry carries the
    OpenRouter base URL, model_id, and required HTTP-Referer / X-Title headers.
    """
    # OMN-17372: both credentials resolve from their secret ref through the
    # store, with no ``env_var_fallback``. See the module docstring — that
    # parameter named a house variable and bypassed the lane secret mapping on
    # the way to ``os.environ``.
    glm_key = resolve_api_key("llm.glm.api_key", required=False)
    model_configs = _build_static_model_configs(glm_key)
    openrouter_key = resolve_api_key("llm.openrouter.api_key", required=False)
    _register_openrouter_models(model_configs, openrouter_key)
    return ModelInferenceBridgeConfig(model_configs=model_configs)


async def load_inference_bridge_config_from_env_async() -> ModelInferenceBridgeConfig:
    """Async variant of :func:`load_inference_bridge_config_from_env`.

    Identical wiring, but resolves the OpenRouter key through
    ``resolve_api_key_async`` so it is safe to call from inside a running event
    loop (the runtime auto-wiring boot path, ``HandlerSegmentation.handle``).
    The static ``LLM_*_URL`` env reads are pure and shared with the sync loader.
    """
    # OMN-17372: both credentials resolve from their secret ref through the
    # store, with no ``env_var_fallback`` — same reason as the sync loader.
    glm_key = await resolve_api_key_async("llm.glm.api_key", required=False)
    model_configs = _build_static_model_configs(glm_key)
    openrouter_key = await resolve_api_key_async(
        "llm.openrouter.api_key", required=False
    )
    _register_openrouter_models(model_configs, openrouter_key)
    return ModelInferenceBridgeConfig(model_configs=model_configs)


def _build_static_model_configs(
    glm_key: SecretStr | None = None,
) -> dict[str, dict[str, object]]:
    """Build the env-driven static model configs for the endpoint metadata.

    Pure with respect to the secret store — it reads only the non-secret
    ``LLM_*_URL`` / model-id env vars and never resolves a credential itself —
    so it is shared verbatim by the sync and async loaders, which each resolve
    ``glm_key`` through the store in the posture that suits them and pass the
    resolved value in.

    OMN-17372: GLM's key used to be read here as a bare
    ``os.environ.get("LLM_GLM_API_KEY")``. That was a raw ambient house
    credential — no secret store in the path at all, so unlike every other
    credential on this boundary it honoured neither the lane secret mapping nor
    any per-tenant scoping, and it named a house variable literally. It now
    arrives as ``glm_key``, resolved from the ``llm.glm.api_key`` secret ref
    like OpenRouter's. On a local install the convention store still maps that
    ref onto ``LLM_GLM_API_KEY``, so a developer's own key resolves exactly as
    before; on a deployed lane it resolves through the lane mapping, which is
    where the house entry was deleted.
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

        if key == "glm" and glm_key is not None:
            resolved = glm_key.get_secret_value().strip()
            if resolved:
                cfg["api_key"] = resolved

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
            "OpenRouter is configured (llm.openrouter.api_key resolved) but "
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
