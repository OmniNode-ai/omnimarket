# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_consumer_group_guard_effect --bus-lane dev``: the consumer-group guard command line."""

from omnimarket.nodes.node_consumer_group_guard_effect.runtime_consumer_group_guard import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
