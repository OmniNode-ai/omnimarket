# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ledger_hold_verdict_effect``."""

import sys

from omnimarket.nodes.node_ledger_hold_verdict_effect.runtime_hold_verdict import main

if __name__ == "__main__":
    sys.exit(main())
