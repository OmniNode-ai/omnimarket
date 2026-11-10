# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_installer_container_units <install|status|uninstall|dry-run> --host H --unit U``."""

import sys

from omnimarket.nodes.node_host_installer_container_units.runtime_container_units import (
    main,
)

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
