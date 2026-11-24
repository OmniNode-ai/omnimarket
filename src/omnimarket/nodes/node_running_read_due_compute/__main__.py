# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_running_read_due_compute <write-due|read-due>`` (OMN-26433)."""

import sys

from omnimarket.nodes.node_running_read_due_compute.runtime_running_read_due import main

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
