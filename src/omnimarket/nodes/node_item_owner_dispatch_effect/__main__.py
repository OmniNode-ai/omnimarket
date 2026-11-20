# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_item_owner_dispatch_effect <plan|place|tick|read> --omni-home <root>``."""

import sys

from omnimarket.nodes.node_item_owner_dispatch_effect.runtime_item_owner_dispatch import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
