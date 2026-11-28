# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_observability_install_effect <dry-run|install|status|readback> --host H``."""

import sys

from omnimarket.nodes.node_host_observability_install_effect.runtime_observability_install import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
