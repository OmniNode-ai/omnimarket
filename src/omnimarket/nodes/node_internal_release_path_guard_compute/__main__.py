# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""JSON adapter for check callers that cannot import the installed node."""

from __future__ import annotations

import sys

from pydantic import ValidationError

from omnimarket.models.model_internal_release_path_guard import (
    ModelInternalReleasePathGuardRequest,
)
from omnimarket.nodes.node_internal_release_path_guard_compute import (
    NodeInternalReleasePathGuardCompute,
)


def main() -> int:
    try:
        request = ModelInternalReleasePathGuardRequest.model_validate_json(
            sys.stdin.read()
        )
    except ValidationError as exc:
        sys.stderr.write(f"internal-release-path: invalid request: {exc}\n")
        return 2
    result = NodeInternalReleasePathGuardCompute().handle(request)
    sys.stdout.write(result.model_dump_json() + "\n")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
