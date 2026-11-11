# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_delegation_code_step_order_effect (OMN-23712).

Usage:
    python -m omnimarket.nodes.node_delegation_code_step_order_effect < request.json

Reads one ModelCodeStepOrderRequest as JSON on stdin and writes the
ModelCodeStepOrderResult as JSON on stdout. Exit 0 when an order was resolved,
3 on a typed refusal: the caller stops with it and never runs a default order.
"""

from __future__ import annotations

import sys

from omnimarket.nodes.node_delegation_code_step_order_effect.handlers.handler_code_step_order import (
    HandlerCodeStepOrder,
)
from omnimarket.nodes.node_delegation_code_step_order_effect.models.model_code_step_order import (
    ModelCodeStepOrderRequest,
)

_EXIT_REFUSED = 3


def main() -> int:
    request = ModelCodeStepOrderRequest.model_validate_json(sys.stdin.read())
    result = HandlerCodeStepOrder().handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.ok else _EXIT_REFUSED


if __name__ == "__main__":
    raise SystemExit(main())
