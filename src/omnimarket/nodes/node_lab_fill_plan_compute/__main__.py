# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CLI entry point for the lab-fill lane brief render (OMN-22866).

Usage:
    python -m omnimarket.nodes.node_lab_fill_plan_compute < request.json > lane.md

Reads one ModelLabFillLaneRenderRequest as JSON on stdin and writes the lane's task brief on stdout. The lab-fill
workflow's dispatch agent runs it once per lane; the workflow script no longer carries a copy of the brief text.
Exit 0 when the brief was rendered, 2 when the request does not validate (the reason is on stderr).
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from omnimarket.nodes.node_lab_fill_plan_compute.handlers.handler_lab_fill_lane_render import (
    HandlerLabFillLaneRender,
)
from omnimarket.nodes.node_lab_fill_plan_compute.models.model_lab_fill_lane_render import (
    ModelLabFillLaneRenderRequest,
)

_EXIT_INVALID = 2


def main() -> int:
    try:
        request = ModelLabFillLaneRenderRequest.model_validate_json(sys.stdin.read())
    except ValidationError as error:
        sys.stderr.write(f"lab-fill lane render: invalid request: {error}\n")
        return _EXIT_INVALID
    sys.stdout.write(HandlerLabFillLaneRender().handle(request).brief)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
