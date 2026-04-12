# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_session_post_mortem — Session post-mortem collector WorkflowPackage."""

from omnimarket.nodes.node_session_post_mortem.handlers.handler_session_post_mortem import (
    HandlerSessionPostMortem,
    ModelPostMortemCommand,
    ModelPostMortemHandlerResult,
)

__all__ = [
    "NodeSessionPostMortem",
    "HandlerSessionPostMortem",
    "ModelPostMortemCommand",
    "ModelPostMortemHandlerResult",
]

class NodeSessionPostMortem(HandlerSessionPostMortem):
    """ONEX entry-point wrapper for HandlerSessionPostMortem."""
