# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the nightly code graph, a probe, a pull request check, or the schedule verbs."""

from omnimarket.nodes.node_code_graph_effect.runtime_code_graph import main

if __name__ == "__main__":
    raise SystemExit(main())
