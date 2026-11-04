# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative operator-host silent-failure alarm effect."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeSilentFailureAlarmEffect(NodeEffect):
    """All alarm work is routed by contract.yaml to HandlerSilentFailureAlarm."""
