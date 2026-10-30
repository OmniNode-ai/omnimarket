# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_plans_today_effect``: the plans-today command line."""

from omnimarket.nodes.node_plans_today_effect.handlers.handler_plans_today_cli import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
