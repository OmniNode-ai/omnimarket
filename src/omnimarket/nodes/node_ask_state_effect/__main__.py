# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_ask_state_effect --since <UTC stamp>``: the ask-state command line."""

from omnimarket.nodes.node_ask_state_effect.runtime_ask_state import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
