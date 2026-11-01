# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_bus_audit_compute handlers."""

from omnimarket.nodes.node_bus_audit_compute.handlers.handler_bus_audit_compute import (
    HandlerBusAuditCompute,
)


def build_handler_bus_audit_compute() -> HandlerBusAuditCompute:
    """Build the handler with its constructor defaults."""
    return HandlerBusAuditCompute()
