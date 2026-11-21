# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_fable_review_gate_compute: the gate decision of operator ruling 2026-11-14T23:50:35Z (OMN-24533).

Every Fable diff review runs only on the orchestrating Mac. This node is the admission decision: given the
logical route keys of a review request and the host the review would run on, it admits or refuses with a typed
reason.
"""

from omnimarket.nodes.node_fable_review_gate_compute.handlers.handler_fable_review_gate import (
    HandlerFableReviewGate,
)

__all__ = ["HandlerFableReviewGate"]
