# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative host deployment node; its contract owns routing and behavior."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeRuntimeDeployEffect(NodeEffect):
    """Deploy the requested infra checkout on the host owning its Docker daemon."""
