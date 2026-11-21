# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_review_lock_effect <acquire|check-scope>``."""

import sys

from omnimarket.nodes.node_review_lock_effect.runtime_review_lock import main

if __name__ == "__main__":
    sys.exit(main())
