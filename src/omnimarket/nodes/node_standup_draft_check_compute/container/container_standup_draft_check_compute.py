# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler factories for node_standup_draft_check_compute."""

from __future__ import annotations

from omnimarket.nodes.node_standup_draft_check_compute.handlers.handler_standup_draft_check import (
    HandlerStandupDraftCheck,
)

__all__ = ["create_handler_standup_draft_check"]


def create_handler_standup_draft_check() -> HandlerStandupDraftCheck:
    """Build the handler with its default dependencies."""
    return HandlerStandupDraftCheck()
