# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""RSD fill compute node - selects top-N tickets by RSD score."""

from __future__ import annotations

from omnibase_core.nodes.node_compute import NodeCompute


class NodeRsdFillCompute(NodeCompute):
    """Declarative compute node for RSD-based ticket selection.

    All behavior is defined in contract.yaml - no custom logic here.
    """

    # Pure declarative shell - all behavior defined in contract.yaml


__all__ = ["NodeRsdFillCompute"]
