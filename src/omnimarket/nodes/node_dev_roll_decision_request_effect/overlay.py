# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The decision-request policy of node_dev_roll_decision_request_effect, from the overlay."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_roll_decision_request_effect.models import (
    ModelDevRollDecisionRequestPolicy,
)

NODE_NAME = "node_dev_roll_decision_request_effect"


def load_decision_request_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelDevRollDecisionRequestPolicy:
    """Read the node's policy from its overlay (no packaged fallback)."""
    return load_structured_overlay(
        NODE_NAME, ModelDevRollDecisionRequestPolicy, environ=environ
    )
