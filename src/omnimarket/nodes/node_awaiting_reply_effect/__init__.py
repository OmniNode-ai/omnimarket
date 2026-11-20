# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Awaiting-reply effect node: sends the AUTO replies and logs each on the ledger."""

from omnimarket.nodes.node_awaiting_reply_effect.handlers.handler_awaiting_reply_send import (
    HandlerAwaitingReplySend,
)


class NodeAwaitingReplyEffect(HandlerAwaitingReplySend):
    """ONEX entrypoint for the awaiting-reply sends."""


__all__ = ["HandlerAwaitingReplySend", "NodeAwaitingReplyEffect"]
