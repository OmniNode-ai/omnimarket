# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lane_work_event_classify_compute``: classify one request from stdin.

Reads one ``ModelLaneWorkClassifyRequest`` as JSON on stdin and prints the
``ModelLaneWorkClassifyAnswer`` as JSON on stdout. Exit 0 on an answer, 2 when the
request does not parse.
"""

from __future__ import annotations

import sys

from pydantic import ValidationError

from omnimarket.nodes.node_lane_work_event_classify_compute.handlers.handler_lane_work_event_classify import (
    HandlerLaneWorkEventClassify,
)
from omnimarket.nodes.node_lane_work_event_classify_compute.models.model_lane_work_classify_request import (
    ModelLaneWorkClassifyRequest,
)


def main() -> int:
    try:
        request = ModelLaneWorkClassifyRequest.model_validate_json(sys.stdin.read())
    except ValidationError as exc:
        sys.stderr.write(f"request does not parse: {exc}\n")
        return 2
    answer = HandlerLaneWorkEventClassify().handle(request)
    sys.stdout.write(answer.model_dump_json() + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
