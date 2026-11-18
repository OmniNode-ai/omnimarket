# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ledger_rows_loader_effect``."""

import sys

from omnimarket.nodes.node_ledger_rows_loader_effect.runtime_rows_loader import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
