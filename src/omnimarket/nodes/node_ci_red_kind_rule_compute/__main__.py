# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read one rule request on stdin, write the answer JSON on stdout."""

from __future__ import annotations

import json
import sys

from omnimarket.nodes.node_ci_red_kind_rule_compute.handlers.handler_ci_red_kind_rule import (
    HandlerCiRedKindRule,
)
from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_request import (
    ModelCiRedKindRuleRequest,
)


def main() -> int:
    raw = sys.stdin.read()
    request = ModelCiRedKindRuleRequest.model_validate(json.loads(raw))
    answer = HandlerCiRedKindRule().handle(request)
    sys.stdout.write(answer.model_dump_json())
    sys.stdout.write("\n")
    return 0 if answer.ok else 3


if __name__ == "__main__":
    sys.exit(main())
