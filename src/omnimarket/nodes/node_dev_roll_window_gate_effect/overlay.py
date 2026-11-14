# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate policy of node_dev_roll_window_gate_effect, from the overlay."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollWindowGatePolicy,
)

NODE_NAME = "node_dev_roll_window_gate_effect"


def load_gate_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelDevRollWindowGatePolicy:
    """Read the node's policy from its overlay (no packaged fallback)."""
    return load_structured_overlay(
        NODE_NAME, ModelDevRollWindowGatePolicy, environ=environ
    )
