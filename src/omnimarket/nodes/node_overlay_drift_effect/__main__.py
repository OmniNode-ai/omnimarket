# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the daily overlay drift read, a probe, or the schedule verbs on this host."""

from omnimarket.nodes.node_overlay_drift_effect.runtime_overlay_drift import main

if __name__ == "__main__":
    raise SystemExit(main())
