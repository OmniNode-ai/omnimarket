# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Answer one plan-assign command from the operator's shell, or probe the overlay."""

from omnimarket.nodes.node_tenant_plan_assign_effect.runtime_tenant_plan_assign import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
