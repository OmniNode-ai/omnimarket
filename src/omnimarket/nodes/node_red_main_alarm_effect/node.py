# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node for the red-main alarm on the runtime tick."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeRedMainAlarmEffect(NodeEffect):
    """The runtime owns the runtime-tick subscription from the contract."""
