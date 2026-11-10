# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_workflow_routing_scan_compute``: one JSON request on stdin, one JSON scan result on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_workflow_routing_scan_compute.handlers import (
    HandlerWorkflowRoutingScan,
)
from omnimarket.nodes.node_workflow_routing_scan_compute.models import (
    ModelWorkflowRoutingScanRequest,
)


def main() -> int:
    request = ModelWorkflowRoutingScanRequest.model_validate_json(sys.stdin.read())
    sys.stdout.write(HandlerWorkflowRoutingScan().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
