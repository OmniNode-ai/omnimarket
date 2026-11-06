# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Item state check and recovery dispatch effect."""

from .handlers.handler_item_state_check import HandlerItemStateCheck


class NodeItemStateCheckEffect(HandlerItemStateCheck):
    """Declarative runtime entry point."""
