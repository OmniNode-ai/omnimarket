# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_broker_acl_overlay_render_compute`` entry point."""

import sys

from omnimarket.nodes.node_broker_acl_overlay_render_compute.runtime_broker_acl_overlay_render import (
    main,
)

if __name__ == "__main__":
    sys.exit(main())
