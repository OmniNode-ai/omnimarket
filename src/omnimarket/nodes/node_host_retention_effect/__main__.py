# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_host_retention_effect run --host <name>``."""

import sys

from omnimarket.nodes.node_host_retention_effect.runtime_host_retention import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
