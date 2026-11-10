# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Broker ACL overlay render compute: render desired ACL rows from an overlay."""

from omnimarket.nodes.node_broker_acl_overlay_render_compute.handlers.handler_broker_acl_overlay_render import (
    BrokerAclOverlayRenderError,
    HandlerBrokerAclOverlayRender,
)
from omnimarket.nodes.node_broker_acl_overlay_render_compute.overlay import (
    OVERLAY_ID_PATTERN,
    load_acl_overlay,
)


class NodeBrokerAclOverlayRender(HandlerBrokerAclOverlayRender):
    """ONEX entry-point wrapper for HandlerBrokerAclOverlayRender."""


__all__ = [
    "BrokerAclOverlayRenderError",
    "HandlerBrokerAclOverlayRender",
    "NodeBrokerAclOverlayRender",
    "OVERLAY_ID_PATTERN",
    "load_acl_overlay",
]
