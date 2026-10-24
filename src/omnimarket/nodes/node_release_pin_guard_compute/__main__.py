# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON adapter for callers that cannot import the installed node."""

from __future__ import annotations

import sys

from pydantic import ValidationError

from omnimarket.models.model_release_pin_guard import ModelReleasePinGuardRequest
from omnimarket.nodes.node_release_pin_guard_compute import NodeReleasePinGuardCompute


def main() -> int:
    try:
        request = ModelReleasePinGuardRequest.model_validate_json(sys.stdin.read())
    except ValidationError as exc:
        sys.stderr.write(f"release-pin-guard: invalid request: {exc}\n")
        return 2
    result = NodeReleasePinGuardCompute().handle(request)
    sys.stdout.write(result.model_dump_json() + "\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
