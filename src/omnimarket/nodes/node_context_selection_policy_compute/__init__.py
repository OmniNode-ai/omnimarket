# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""node_context_selection_policy_compute — policy-based, reason-annotated context selection.

Pure COMPUTE node implementing the Context Authority Rule (OMN-12843 / M3):
ranks candidate context factors by measured effectiveness (resolved from the M2
capsule store, passed in) and stamps every selection with the Authority 5-tuple.
"""

from omnimarket.nodes.node_context_selection_policy_compute.handlers.handler_context_selection_policy import (
    ContextSelectionPolicyError,
    HandlerContextSelectionPolicy,
)

__all__ = [
    "ContextSelectionPolicyError",
    "HandlerContextSelectionPolicy",
]
