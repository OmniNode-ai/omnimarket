# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_skill_tree_read_effect."""

from __future__ import annotations

from omnimarket.nodes.node_skill_tree_read_effect.handlers.handler_skill_tree_read import (
    HandlerSkillTreeRead,
)

__all__ = ["create_handler_skill_tree_read"]


def create_handler_skill_tree_read() -> HandlerSkillTreeRead:
    """Build the handler with its default dependencies."""
    return HandlerSkillTreeRead()
