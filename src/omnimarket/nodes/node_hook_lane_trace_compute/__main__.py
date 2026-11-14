# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Print the lane trace query, or the trace of the rows it returned (OMN-24072)."""

from omnimarket.nodes.node_hook_lane_trace_compute.runtime_hook_lane_trace import main

if __name__ == "__main__":
    raise SystemExit(main())
