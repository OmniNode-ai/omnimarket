# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the broker topic delete named by a command file on this host."""

from omnimarket.nodes.node_broker_topic_delete_effect.runtime_broker_topic_delete import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
