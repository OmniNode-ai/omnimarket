# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Canonical node for the third-lease stop (OMN-21955)."""

from omnimarket.nodes.node_third_lease_stop_compute.handlers.handler_third_lease_stop import (
    HandlerThirdLeaseStop,
)


class NodeThirdLeaseStopCompute(HandlerThirdLeaseStop):
    """Declarative entry point; all policy lives in the pure handler and the contract."""


__all__ = ["HandlerThirdLeaseStop", "NodeThirdLeaseStopCompute"]
