# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide one overlay change: ``python -m omnimarket.nodes.node_overlay_merge_order_compute < command.json``."""

from omnimarket.nodes.node_overlay_merge_order_compute.runtime_overlay_merge_order import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
