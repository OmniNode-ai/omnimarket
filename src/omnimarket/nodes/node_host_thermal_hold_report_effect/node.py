# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative host thermal hold report effect: all behaviour is in the contract and its handler."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeHostThermalHoldReportEffect(NodeEffect):
    """Deliver a host thermal guard's queued rows and alarm once per hold episode."""
