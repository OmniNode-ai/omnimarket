# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An operator ask's done check reads the running system and records its verdict."""

from omnimarket.nodes.node_ask_done_check_effect.handlers import HandlerAskDoneCheck


class NodeAskDoneCheckEffect(HandlerAskDoneCheck):
    """ONEX entry point."""


__all__ = ["HandlerAskDoneCheck", "NodeAskDoneCheckEffect"]
