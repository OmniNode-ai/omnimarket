# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute the tool outcome for each JSON Lines request on stdin."""

from omnimarket.nodes.node_hook_tool_outcome_compute.runtime_hook_tool_outcome import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
