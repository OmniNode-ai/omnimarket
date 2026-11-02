# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_retrospective_audit_compute: scaffold from the weekly_retrospective design."""

from omnimarket.nodes.node_retrospective_audit_compute.handlers.handler_retrospective_audit_compute import (
    HandlerRetrospectiveAuditCompute,
)

__all__ = ["HandlerRetrospectiveAuditCompute", "NodeRetrospectiveAuditCompute"]


class NodeRetrospectiveAuditCompute(HandlerRetrospectiveAuditCompute):
    """ONEX entry-point wrapper for HandlerRetrospectiveAuditCompute."""
