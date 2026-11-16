# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the daily pilot measurement read, or a probe, on this host."""

from omnimarket.nodes.node_pilot_measurement_effect.runtime_pilot_measurement import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
