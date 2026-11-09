# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_landing_private_reads_effect``: run the day-one listing once."""

import sys

from omnimarket.nodes.node_landing_private_reads_effect.runtime_private_reads import (
    main,
)

sys.exit(main())
