# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Inference Bridge Adapter — bridges orchestrator to node_llm_inference_effect.

Shared location for AdapterInferenceBridge so multiple nodes (hostile_reviewer,
pr_review_bot, adr_canary, etc.) can use it without cross-node reach-in.
"""

from omnimarket.nodes.node_hostile_reviewer.handlers.adapter_inference_bridge import (
    AdapterInferenceBridge,
    ModelInferenceAdapter,
    ModelInferenceBridgeConfig,
)

__all__: list[str] = [
    "AdapterInferenceBridge",
    "ModelInferenceAdapter",
    "ModelInferenceBridgeConfig",
]
