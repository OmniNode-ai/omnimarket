# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read PR facts from stdin and print the reusable check-title step's output."""

from __future__ import annotations

import json
import sys

from pydantic import ValidationError

from omnimarket.nodes.node_pr_title_check_compute.handlers.handler_pr_title_check import (
    HandlerPrTitleCheck,
)
from omnimarket.nodes.node_pr_title_check_compute.models.model_pr_title_check import (
    ModelPrTitleCheckRequest,
)


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        request = ModelPrTitleCheckRequest.model_validate(payload)
    except (json.JSONDecodeError, ValidationError) as exc:
        sys.stderr.write(f"PR_TITLE_CHECK_BAD_REQUEST:{exc}\n")
        return 2

    result = HandlerPrTitleCheck().handle(request)
    for line in result.output_lines:
        sys.stdout.write(line + "\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
