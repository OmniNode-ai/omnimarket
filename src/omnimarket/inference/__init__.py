# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared inference wiring helpers for omnimarket nodes.

``bridge_config_loader`` builds a ``ModelInferenceBridgeConfig`` populated
from ``LLM_*_URL`` / ``LLM_*_MODEL_NAME`` env vars so reviewer nodes
(node_pr_review_bot, node_hostile_reviewer) can resolve caller-supplied
model keys without per-node registry duplication.
"""

from omnimarket.inference.bridge_config_loader import (
    ConfigError,
    load_inference_bridge_config,
    load_inference_bridge_config_from_env,
)

__all__: list[str] = [
    "ConfigError",
    "load_inference_bridge_config",
    "load_inference_bridge_config_from_env",
]
