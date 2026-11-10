# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""python -m omnimarket.nodes.node_pr_landing_snapshot_effect: one pass on the real backend (OMN-23409)."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_pr_landing_snapshot_effect.runtime_snapshot import main

raise SystemExit(main(sys.argv[1:]))
