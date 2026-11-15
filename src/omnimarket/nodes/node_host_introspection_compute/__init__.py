# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_host_introspection_compute: a lab host's announcement and the digest of its facts (OMN-24367)."""

from omnimarket.nodes.node_host_introspection_compute.handlers.handler_host_introspection import (
    HandlerHostIntrospection,
)


class NodeHostIntrospectionCompute(HandlerHostIntrospection):
    """ONEX entry-point wrapper for HandlerHostIntrospection."""


__all__ = ["HandlerHostIntrospection", "NodeHostIntrospectionCompute"]
