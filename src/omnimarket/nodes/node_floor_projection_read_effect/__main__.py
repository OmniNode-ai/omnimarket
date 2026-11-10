# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_floor_projection_read_effect --home <registry home>``."""

import sys

from omnimarket.nodes.node_floor_projection_read_effect.runtime_floor_projection_read import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
