# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRrhCompute — Release Readiness Handshake validation.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
STUB — not yet implemented (OMN-12231).
"""

from __future__ import annotations

from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_request import (
    ModelRrhComputeRequest,
)
from omnimarket.nodes.node_rrh_compute.models.model_rrh_compute_result import (
    ModelRrhComputeResult,
)


class HandlerRrhCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelRrhComputeRequest) -> ModelRrhComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_rrh_compute is not yet implemented (OMN-12231). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
