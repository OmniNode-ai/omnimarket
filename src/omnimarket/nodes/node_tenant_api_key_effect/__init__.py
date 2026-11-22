# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registers and revokes gateway keys by digest (plan task T7)."""

from omnimarket.nodes.node_tenant_api_key_effect.handlers import (
    HandlerTenantApiKeyRegister,
    HandlerTenantApiKeyRevoke,
)


class NodeTenantApiKeyEffect(HandlerTenantApiKeyRegister):
    """ONEX entry point."""


__all__ = [
    "HandlerTenantApiKeyRegister",
    "HandlerTenantApiKeyRevoke",
    "NodeTenantApiKeyEffect",
]
