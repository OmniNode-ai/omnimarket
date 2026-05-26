# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerRewindCompute — Query event stream by agent identity and timestamp window.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
STUB — not yet implemented (OMN-12231).
"""

from __future__ import annotations

from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_request import (
    ModelRewindComputeRequest,
)
from omnimarket.nodes.node_rewind_compute.models.model_rewind_compute_result import (
    ModelRewindComputeResult,
)


class HandlerRewindCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelRewindComputeRequest) -> ModelRewindComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_rewind_compute is not yet implemented (OMN-12231). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
