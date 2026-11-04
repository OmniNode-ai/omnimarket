# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_linear_snapshot_effect``: write today's Linear snapshot JSON."""

from .runtime_linear_snapshot import main

if __name__ == "__main__":
    raise SystemExit(main())
