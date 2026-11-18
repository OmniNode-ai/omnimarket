# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the host local config drift read: ``python -m omnimarket.nodes.node_host_local_config_drift_compute --host <name> --correlation-id <id>``."""

from omnimarket.nodes.node_host_local_config_drift_compute.runtime_host_local_config_drift import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
