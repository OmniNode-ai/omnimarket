# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of node_pool_usage_verdict_compute: every verdict threshold, none in code.

Found at ``<root>/node_pool_usage_verdict_compute/overlay.yaml`` under a root named in
``ONEX_SKILL_OVERLAY_ROOTS``. The package ships ``overlay.example.yaml`` with example values;
the deployed values live in the overlays repository. A missing overlay refuses the load —
the verdict never falls back to a threshold spelled in code.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_pool_usage_verdict_compute.models.model_pool_usage_policy import (
    ModelPoolUsagePolicy,
)

NODE_NAME = "node_pool_usage_verdict_compute"


def load_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelPoolUsagePolicy:
    """The verdict thresholds; a missing overlay refuses rather than defaulting."""
    return load_structured_overlay(NODE_NAME, ModelPoolUsagePolicy, environ)
