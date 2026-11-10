# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lane_parent_notice_recovery_effect consume|lanes --ledger <file>``."""

from omnimarket.nodes.node_lane_parent_notice_recovery_effect.runtime_notice_recovery import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
