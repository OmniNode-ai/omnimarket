# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the delegation check regression alarm on this host."""

from omnimarket.nodes.node_delegation_check_regression_effect.runtime_delegation_check_regression import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
