# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The capture policy of node_dev_roll_failure_capture_effect, from the overlay."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_roll_failure_capture_effect.models import (
    ModelDevRollCapturePolicy,
)

NODE_NAME = "node_dev_roll_failure_capture_effect"


def load_capture_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelDevRollCapturePolicy:
    """Read the node's capture policy from its overlay (no packaged fallback)."""
    return load_structured_overlay(
        NODE_NAME, ModelDevRollCapturePolicy, environ=environ
    )
