# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_workflow_baseline_compare_compute``: one JSON request on stdin, one JSON receipt on stdout."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_workflow_baseline_compare_compute.handlers import (
    HandlerWorkflowBaselineCompare,
)
from omnimarket.nodes.node_workflow_baseline_compare_compute.models import (
    ModelWorkflowBaselineCompareRequest,
)


def main() -> int:
    request = ModelWorkflowBaselineCompareRequest.model_validate_json(sys.stdin.read())
    sys.stdout.write(HandlerWorkflowBaselineCompare().handle(request).model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
