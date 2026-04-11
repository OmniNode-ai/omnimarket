# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Topic constants for node_model_router.

Declared in contract.yaml publish_topics. Reference these constants in
handler code — never inline topic strings directly.

Related:
    - OMN-8443: W1.2 contract-driven model routing substrate
"""

from __future__ import annotations

TOPIC_MODEL_ROUTING_DEGRADED = "onex.evt.omnimarket.model-routing.degraded.v1"

__all__: list[str] = ["TOPIC_MODEL_ROUTING_DEGRADED"]
