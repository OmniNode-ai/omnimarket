# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ItemUnanswered compute node."""

from .handlers.handler_item_unanswered import HandlerItemUnanswered


class NodeItemUnansweredCompute(HandlerItemUnanswered):
    """Declarative runtime entry point."""
