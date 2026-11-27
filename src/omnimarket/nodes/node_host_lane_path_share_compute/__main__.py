# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_lane_path_share_compute < request.json``: print the result and its STATUS rows."""

import sys

from omnimarket.nodes.node_host_lane_path_share_compute.models.model_lane_path_share import (
    ModelLanePathShareRequest,
)
from omnimarket.nodes.node_host_lane_path_share_compute.node import (
    NodeHostLanePathShareCompute,
)


def main() -> int:
    request = ModelLanePathShareRequest.model_validate_json(sys.stdin.read())
    result = NodeHostLanePathShareCompute().handle(request)
    sys.stdout.write(result.model_dump_json(indent=1) + "\n")
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
