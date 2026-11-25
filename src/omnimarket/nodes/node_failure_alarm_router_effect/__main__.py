# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one fire from a JSON request on stdin; print the typed result. Exit 3 on a loud refusal."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_failure_alarm_router_effect.handler import (
    HandlerFailureAlarmRouter,
    PositiveControlFailedError,
    SelfCheckFailedError,
)
from omnimarket.nodes.node_failure_alarm_router_effect.models import (
    ModelFailureAlarmRequest,
)


def main() -> int:
    request = ModelFailureAlarmRequest.model_validate_json(sys.stdin.read())
    try:
        result = HandlerFailureAlarmRouter().handle(request)
    except (PositiveControlFailedError, SelfCheckFailedError) as exc:
        sys.stderr.write(f"REFUSED: {exc}\n")
        return 3
    sys.stdout.write(result.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
