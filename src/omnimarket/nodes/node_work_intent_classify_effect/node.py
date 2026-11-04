# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeWorkIntentClassifyEffect - contract-driven effect node (OMN-22263)."""

from __future__ import annotations

from omnibase_core.nodes.node_effect import NodeEffect


class NodeWorkIntentClassifyEffect(NodeEffect):
    """The node; its logic lives in the handler its contract names."""


__all__ = ["NodeWorkIntentClassifyEffect"]
