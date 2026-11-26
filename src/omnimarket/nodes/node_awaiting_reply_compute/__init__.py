# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Awaiting-reply compute node: which asks wait on the operator and which replies are AUTO."""

from omnimarket.nodes.node_awaiting_reply_compute.handlers.handler_awaiting_reply import (
    HandlerAwaitingReply,
)


class NodeAwaitingReplyCompute(HandlerAwaitingReply):
    """ONEX entrypoint for the awaiting-reply decisions."""


__all__ = ["HandlerAwaitingReply", "NodeAwaitingReplyCompute"]
