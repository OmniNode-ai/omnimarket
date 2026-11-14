# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_introspection_compute: one host's introspection announcement from its readings (OMN-19998)."""

from omnimarket.nodes.node_host_introspection_compute.handlers.handler_host_introspection import (
    HandlerHostIntrospection,
    HostIntrospectionRefusedError,
)


class NodeHostIntrospectionCompute(HandlerHostIntrospection):
    """ONEX entry-point wrapper for HandlerHostIntrospection."""


__all__ = [
    "HandlerHostIntrospection",
    "HostIntrospectionRefusedError",
    "NodeHostIntrospectionCompute",
]
