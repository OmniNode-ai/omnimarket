# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the broker load reading (probe or daily) on this host."""

from omnimarket.nodes.node_broker_load_reading_effect.runtime_broker_load import main

if __name__ == "__main__":
    raise SystemExit(main())
