# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from omnimarket.nodes.node_internal_release_path_guard_compute import (
    NodeInternalReleasePathGuardCompute,
)


def build_node_internal_release_path_guard_compute() -> (
    NodeInternalReleasePathGuardCompute
):
    """Build the handler with its constructor defaults."""
    return NodeInternalReleasePathGuardCompute()
