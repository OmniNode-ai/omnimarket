# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tailnet https listener in front of a loopback upstream, with refusals, a readback and a rollback."""

from omnimarket.nodes.node_tailnet_https_serve_effect.handlers import (
    HandlerTailnetHttpsServe,
)


class NodeTailnetHttpsServeEffect(HandlerTailnetHttpsServe):
    """ONEX entry point."""


__all__ = ["HandlerTailnetHttpsServe", "NodeTailnetHttpsServeEffect"]
