# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the overlay consumer join check: ``python -m omnimarket.nodes.node_overlay_drift_compute <daily|probe>``."""

from omnimarket.nodes.node_overlay_drift_compute.runtime_overlay_drift_compute import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
