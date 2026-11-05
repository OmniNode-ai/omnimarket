# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the hourly host readings, a probe, or the schedule verbs on this host."""

from omnimarket.nodes.node_host_reading_effect.runtime_host_reading import main

if __name__ == "__main__":
    raise SystemExit(main())
