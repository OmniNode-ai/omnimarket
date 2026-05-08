# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared inference wiring helpers for omnimarket nodes.

``bridge_config_loader`` builds a ``ModelInferenceBridgeConfig`` populated
from ``LLM_*_URL`` / ``LLM_*_MODEL_NAME`` env vars so reviewer nodes
(node_pr_review_bot, node_hostile_reviewer) can resolve caller-supplied
model keys without per-node registry duplication.

``openrouter_models`` defines the free-tier OpenRouter model catalog.
``adapter_inference_bridge`` re-exports the shared bridge adapter.
"""

from omnimarket.inference.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)
from omnimarket.inference.bridge_config_loader import (
    load_inference_bridge_config_from_env,
)
from omnimarket.inference.openrouter_models import (
    EnumModelAvailability,
    EnumOpenRouterTier,
    ModelOpenRouterModelConfig,
    get_openrouter_models,
)

__all__: list[str] = [
    "AdapterInferenceBridge",
    "EnumModelAvailability",
    "EnumOpenRouterTier",
    "ModelInferenceAdapter",
    "ModelInferenceBridgeConfig",
    "ModelOpenRouterModelConfig",
    "get_openrouter_models",
    "load_inference_bridge_config_from_env",
]
