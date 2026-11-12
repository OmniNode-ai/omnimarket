# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_floor_emissions_publish_effect --home <registry home>``."""

import sys

from omnimarket.nodes.node_floor_emissions_publish_effect.runtime_floor_emissions_publish import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
