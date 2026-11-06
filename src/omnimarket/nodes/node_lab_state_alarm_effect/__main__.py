# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_state_alarm_effect --canary-status FILE``: the hosted canary's verdict."""

from __future__ import annotations

import sys

from omnimarket.nodes.node_lab_state_alarm_effect.runtime_canary import main

if __name__ == "__main__":
    sys.exit(main())
