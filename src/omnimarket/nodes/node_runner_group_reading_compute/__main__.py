# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_runner_group_reading_compute``: one JSON request on stdin, one JSON reading on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_runner_group_reading_compute.handlers import (
    HandlerRunnerGroupReading,
)
from omnimarket.nodes.node_runner_group_reading_compute.models import (
    ModelRunnerGroupReadingRequest,
)


def main() -> int:
    request = ModelRunnerGroupReadingRequest.model_validate_json(sys.stdin.read())
    sys.stdout.write(HandlerRunnerGroupReading().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
