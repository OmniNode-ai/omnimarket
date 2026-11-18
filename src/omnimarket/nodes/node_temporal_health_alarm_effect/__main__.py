# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the Temporal health alarm tick as a module (python -m ...)."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_temporal_health_alarm_effect.runtime_temporal_health_alarm import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
