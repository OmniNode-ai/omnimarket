# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run scenario R5 once (``run``) or manage the node's hourly timer (``schedule install|uninstall``)."""

from omnimarket.nodes.node_delegation_scenario_r5_effect.runtime_scenario_r5 import main

if __name__ == "__main__":
    raise SystemExit(main())
