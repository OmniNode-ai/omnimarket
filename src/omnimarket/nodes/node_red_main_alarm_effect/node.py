# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node for the throughput tick's red-main alarm."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeRedMainAlarmEffect(NodeEffect):
    """The runtime owns the throughput fire subscription from the contract."""
