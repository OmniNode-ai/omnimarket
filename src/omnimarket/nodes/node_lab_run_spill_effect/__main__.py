# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_lab_run_spill_effect <action> --host <name> ...``."""

import sys

from omnimarket.nodes.node_lab_run_spill_effect.runtime_lab_run_spill import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
