# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the batching policy from the node's overlay. No packaged fallback value."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_roll_request_compute.models import ModelDevRollPolicy

NODE_NAME = "node_dev_roll_request_compute"


def load_dev_roll_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelDevRollPolicy:
    """Read ``<root>/node_dev_roll_request_compute/overlay.yaml``; absent refuses."""
    return load_structured_overlay(NODE_NAME, ModelDevRollPolicy, environ)
