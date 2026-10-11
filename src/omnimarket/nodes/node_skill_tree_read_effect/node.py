# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node reading a skills tree; the contract routes to the handler."""

from __future__ import annotations

from omnibase_core.nodes.node_effect import NodeEffect


class NodeSkillTreeReadEffect(NodeEffect):
    """Thin shell: HandlerSkillTreeRead, bound in contract.yaml, owns the reading."""


__all__ = ["NodeSkillTreeReadEffect"]
