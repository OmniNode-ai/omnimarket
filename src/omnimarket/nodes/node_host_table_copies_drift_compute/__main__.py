# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the host table copies drift read: ``python -m omnimarket.nodes.node_host_table_copies_drift_compute <daily|probe>``."""

from omnimarket.nodes.node_host_table_copies_drift_compute.runtime_host_table_copies_drift import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
