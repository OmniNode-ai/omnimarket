# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a node whose declared overlay row is absent from the overlays repository."""

from omnimarket.nodes.node_depends_on_overlay_check_effect.runtime_depends_on_overlay_check import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
