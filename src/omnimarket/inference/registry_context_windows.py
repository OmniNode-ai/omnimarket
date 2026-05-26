# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registry-backed context window lookup for inference bridge loaders.

Provides a module-level helper that loads the model registry once at import
time and exposes ``get_context_window_for_endpoint_env`` so callers can replace
hardcoded context window integers with registry-driven lookups.

Usage::

    from omnimarket.inference.registry_context_windows import (
        get_context_window_for_endpoint_env,
    )

    context_window = get_context_window_for_endpoint_env("LLM_CODER_URL", fallback=114688)

``fallback`` is required: it is used for endpoint env vars that are not
represented in the model registry (e.g. frontier cloud models that have no
registry entry yet).  The caller must supply an explicit fallback so the
intent is visible and auditable.

Implementation note: the registry is loaded eagerly at module import.
``FileNotFoundError`` and ``ValidationError`` propagate at startup time so
misconfigured environments fail loudly rather than silently using stale data.
"""

from __future__ import annotations

import logging

from omnimarket.models.delegation.llm_cost_routing.model_llm_model_registry import (
    ModelLlmModelRegistryLoader,
)

logger = logging.getLogger(__name__)

# Load once at module import — same pattern as other registry consumers in this
# codebase.  The loader resolves the path relative to the YAML file on disk via
# _DEFAULT_REGISTRY_PATH in model_llm_model_registry.py.
_registry = ModelLlmModelRegistryLoader().load()

# Build a reverse index: endpoint_env -> context_window.
# When multiple models share the same endpoint_env, take the max (most
# conservative budget preserves correctness).
_endpoint_env_to_context_window: dict[str, int] = {}
for _profile in _registry.models.values():
    _existing = _endpoint_env_to_context_window.get(_profile.endpoint_env, 0)
    if _profile.context_window > _existing:
        _endpoint_env_to_context_window[_profile.endpoint_env] = _profile.context_window

logger.debug(
    "registry_context_windows: loaded %d models, %d endpoint_env entries",
    len(_registry.models),
    len(_endpoint_env_to_context_window),
)


def get_context_window_for_endpoint_env(endpoint_env: str, *, fallback: int) -> int:
    """Return the registry context window for the given endpoint env var name.

    Args:
        endpoint_env: The env var name declared in the registry
            (e.g. ``"LLM_CODER_URL"``).
        fallback: Value to use when ``endpoint_env`` has no registry entry.
            Callers MUST supply an explicit fallback — do NOT pass 0 or a
            silent sentinel.  The fallback is logged at DEBUG level so drift
            between registry and fallback is visible.

    Returns:
        The registry ``context_window`` value, or ``fallback`` if not found.
    """
    value = _endpoint_env_to_context_window.get(endpoint_env)
    if value is None:
        logger.debug(
            "registry_context_windows: no registry entry for endpoint_env=%r, "
            "using fallback=%d",
            endpoint_env,
            fallback,
        )
        return fallback
    return value


def get_context_window_for_model_id(model_id: str, *, fallback: int) -> int:
    """Return the registry context window for the given model_id.

    Args:
        model_id: The canonical model identifier in the registry
            (e.g. ``"qwen3-coder-30b"``).
        fallback: Value to use when ``model_id`` has no registry entry.

    Returns:
        The registry ``context_window`` value, or ``fallback`` if not found.
    """
    profile = _registry.models.get(model_id)
    if profile is None:
        logger.debug(
            "registry_context_windows: no registry entry for model_id=%r, "
            "using fallback=%d",
            model_id,
            fallback,
        )
        return fallback
    return profile.context_window


__all__: list[str] = [
    "get_context_window_for_endpoint_env",
    "get_context_window_for_model_id",
]
