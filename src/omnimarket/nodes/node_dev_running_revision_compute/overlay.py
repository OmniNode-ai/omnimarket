# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the running-revision policy from the node's overlay. No packaged fallback value."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_dev_running_revision_compute.models import (
    ModelRunningRevisionPolicy,
)

NODE_NAME = "node_dev_running_revision_compute"


def load_dev_running_revision_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelRunningRevisionPolicy:
    """Read ``<root>/node_dev_running_revision_compute/overlay.yaml``; absent refuses."""
    return load_structured_overlay(NODE_NAME, ModelRunningRevisionPolicy, environ)
