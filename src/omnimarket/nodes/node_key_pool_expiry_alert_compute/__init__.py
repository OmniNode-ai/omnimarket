# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tenant key pool expiry alert compute node (OMN-20993)."""

from omnimarket.nodes.node_key_pool_expiry_alert_compute.handlers.handler_key_pool_expiry_alert import (
    HandlerKeyPoolExpiryAlert,
)


class NodeKeyPoolExpiryAlertCompute(HandlerKeyPoolExpiryAlert):
    """ONEX entrypoint for the pure key pool expiry alert decision."""


__all__ = ["HandlerKeyPoolExpiryAlert", "NodeKeyPoolExpiryAlertCompute"]
