# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_rsd_migration_orchestrator``: the RSD migration CLI."""

from omnimarket.nodes.node_rsd_migration_orchestrator.runtime_rsd_migration_cli import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
