# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_integration_reading_publish_effect --home <registry home> < batch.json``."""

import sys

from omnimarket.nodes.node_integration_reading_publish_effect.runtime_integration_reading_publish import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
