# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_session_stall_alarm_effect`` runs one tick."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_session_stall_alarm_effect.runtime_session_stall_tick import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
