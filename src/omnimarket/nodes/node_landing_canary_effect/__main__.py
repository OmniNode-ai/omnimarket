# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the landing canary (probe or daily) or manage its schedule on this host."""

from omnimarket.nodes.node_landing_canary_effect.runtime_landing_canary import main

if __name__ == "__main__":
    raise SystemExit(main())
