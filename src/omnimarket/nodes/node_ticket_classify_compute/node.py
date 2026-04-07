# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ticket classify compute node - keyword heuristic buildability classification."""

from __future__ import annotations

from omnibase_core.nodes.node_compute import NodeCompute


class NodeTicketClassifyCompute(NodeCompute):
    """Declarative compute node for ticket buildability classification.

    All behavior is defined in contract.yaml - no custom logic here.
    """

    # Pure declarative shell - all behavior defined in contract.yaml


__all__ = ["NodeTicketClassifyCompute"]
