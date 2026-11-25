# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""``python -m omnimarket.nodes.node_landing_proof_gate_compute``: the gate's one command."""

import sys

from omnimarket.nodes.node_landing_proof_gate_compute.runtime_landing_proof_gate import (
    main,
)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
