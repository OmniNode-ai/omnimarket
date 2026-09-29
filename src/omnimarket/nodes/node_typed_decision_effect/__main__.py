# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for node_typed_decision_effect.

Usage:
    python -m omnimarket.nodes.node_typed_decision_effect < request.json

Reads one ModelTypedDecisionRequest as JSON on stdin and writes the
ModelTypedDecisionResult as JSON on stdout. The result never carries the key:
it is resolved inside the handler from the secret store by the routing
contract's reference, and nothing on the command line or in the output names
its value. The incumbent is optional; without it the request is blind.
Exit 0 when the model decided, 3 when the incumbent answered or there is no answer.
"""

from __future__ import annotations

import sys

from omnimarket.nodes.node_typed_decision_effect.handlers.handler_typed_decision import (
    HandlerTypedDecision,
)
from omnimarket.nodes.node_typed_decision_effect.models.model_typed_decision import (
    EnumTypedDecisionDecider,
    ModelTypedDecisionRequest,
)

_EXIT_NOT_MODEL = 3


def main() -> int:
    request = ModelTypedDecisionRequest.model_validate_json(sys.stdin.read())
    result = HandlerTypedDecision().handle(request)
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0 if result.decided_by is EnumTypedDecisionDecider.MODEL else _EXIT_NOT_MODEL


if __name__ == "__main__":
    raise SystemExit(main())
