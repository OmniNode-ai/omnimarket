# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_factory_floor_unresolved_effect --home <registry home>``."""

import sys

from omnimarket.nodes.node_factory_floor_unresolved_effect.runtime_factory_floor_unresolved import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
