# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative patch apply dispatch node."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodePatchApplyDispatchEffect(NodeEffect):
    """Local reads, lane dispatch and close-out are declared by the sibling contract."""
