# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_proof_judge_compute: the lab proof judge's rule logic (OMN-23642).

Pure handlers (definition-B, no I/O, no clock) for the two extensions the lab
proof judge computes: ``changed_path_live`` and ``focused_tests``. The rule set
is the overlay model; every setting comes from it. The node runs from the pool
driver's own command, not inside a runtime.
"""

from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_changed_path_live import (
    HandlerChangedPathLive,
)
from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_focused_tests import (
    HandlerFocusedTests,
)
from omnimarket.nodes.node_lab_proof_judge_compute.handlers.handler_test_selection import (
    HandlerTestSelection,
)

__all__ = [
    "HandlerChangedPathLive",
    "HandlerFocusedTests",
    "HandlerTestSelection",
]
