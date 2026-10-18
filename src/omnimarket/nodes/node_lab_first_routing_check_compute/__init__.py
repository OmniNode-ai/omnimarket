# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_first_routing_check_compute: refuse a routing change that sends a lab-passing class to a cloud model first (OMN-17427)."""

from omnimarket.nodes.node_lab_first_routing_check_compute.handlers.handler_lab_first_routing_check import (
    HandlerLabFirstRoutingCheck,
)

__all__ = ["HandlerLabFirstRoutingCheck"]
