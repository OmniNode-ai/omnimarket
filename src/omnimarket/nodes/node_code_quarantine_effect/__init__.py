# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Quarantine an unused entity with its tests for 7 days, restore it on a red, remove it with a tag and a catalog row."""

from omnimarket.nodes.node_code_quarantine_effect.handlers import HandlerCodeQuarantine


class NodeCodeQuarantineEffect(HandlerCodeQuarantine):
    """ONEX entry point."""


__all__ = ["HandlerCodeQuarantine", "NodeCodeQuarantineEffect"]
