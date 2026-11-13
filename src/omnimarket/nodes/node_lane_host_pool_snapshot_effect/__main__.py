# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""python -m omnimarket.nodes.node_lane_host_pool_snapshot_effect: one pass on the real backend (OMN-23664)."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.runtime_pool_snapshot import (
    main,
)

raise SystemExit(main(sys.argv[1:]))
