# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read one replay route request on stdin, write the answer JSON on stdout."""

from __future__ import annotations

import json
import sys

from omnimarket.nodes.node_delegation_replay_route_compute.handlers.handler_replay_route import (
    HandlerDelegationReplayRoute,
)
from omnimarket.nodes.node_delegation_replay_route_compute.models.model_replay_route_request import (
    ModelReplayRouteRequest,
)


def main() -> int:
    raw = sys.stdin.read()
    request = ModelReplayRouteRequest.model_validate(json.loads(raw))
    answer = HandlerDelegationReplayRoute().handle(request)
    sys.stdout.write(answer.model_dump_json())
    sys.stdout.write("\n")
    return 0 if answer.ok else 3


if __name__ == "__main__":
    sys.exit(main())
