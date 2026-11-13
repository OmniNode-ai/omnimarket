# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the verify policy from the node's overlay. No packaged fallback value."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_roll_verify_compute.models import (
    ModelDevRollVerifyPolicy,
)

NODE_NAME = "node_dev_roll_verify_compute"


def load_dev_roll_verify_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelDevRollVerifyPolicy:
    """Read ``<root>/node_dev_roll_verify_compute/overlay.yaml``; absent refuses."""
    return load_structured_overlay(NODE_NAME, ModelDevRollVerifyPolicy, environ)
