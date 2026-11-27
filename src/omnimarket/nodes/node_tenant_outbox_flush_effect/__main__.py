# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one tenant outbox flush pass, or probe the overlay."""

from omnimarket.nodes.node_tenant_outbox_flush_effect.runtime_tenant_outbox_flush import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
