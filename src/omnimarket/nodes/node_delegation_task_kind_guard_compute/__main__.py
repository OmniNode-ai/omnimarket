# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Read one guard request on stdin, write the answer JSON on stdout."""

from __future__ import annotations

import json
import sys

from omnimarket.nodes.node_delegation_task_kind_guard_compute.handlers.handler_task_kind_guard import (
    HandlerDelegationTaskKindGuard,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_request import (
    ModelTaskKindGuardRequest,
)


def main() -> int:
    raw = sys.stdin.read()
    request = ModelTaskKindGuardRequest.model_validate(json.loads(raw))
    answer = HandlerDelegationTaskKindGuard().handle(request)
    sys.stdout.write(answer.model_dump_json())
    sys.stdout.write("\n")
    return 0 if answer.ok else 3


if __name__ == "__main__":
    sys.exit(main())
