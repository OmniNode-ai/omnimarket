# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_broker_acl_reconcile_effect --mode dry_run|apply``."""

from omnimarket.nodes.node_broker_acl_reconcile_effect.runtime_broker_acl_reconcile import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
