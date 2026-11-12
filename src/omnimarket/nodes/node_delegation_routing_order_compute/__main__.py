# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_delegation_routing_order_compute (OMN-23712).

Usage:
    python -m omnimarket.nodes.node_delegation_routing_order_compute < request.json

Reads one ModelDelegationRoutingOrderRequest as JSON on stdin and writes the
ModelDelegationRoutingOrderAnswer as JSON on stdout. Exit 0 when an order was
answered, 3 on a typed refusal: the caller stops with it and never runs a
default order.
"""

from __future__ import annotations

import sys

from omnibase_core.models.delegation.model_delegation_routing_order_request import (
    ModelDelegationRoutingOrderRequest,
)

from omnimarket.nodes.node_delegation_routing_order_compute.handlers.handler_delegation_routing_order import (
    HandlerDelegationRoutingOrder,
)

_EXIT_REFUSED = 3


def main() -> int:
    request = ModelDelegationRoutingOrderRequest.model_validate_json(sys.stdin.read())
    answer = HandlerDelegationRoutingOrder().handle(request)
    sys.stdout.write(answer.model_dump_json(indent=2) + "\n")
    return 0 if answer.ok else _EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
