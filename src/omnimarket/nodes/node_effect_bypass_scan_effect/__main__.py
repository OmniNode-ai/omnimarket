# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Scan a checkout for bypasses of the effect nodes, or check the count against its baseline."""

from omnimarket.nodes.node_effect_bypass_scan_effect.runtime_bypass_scan import main

if __name__ == "__main__":
    raise SystemExit(main())
