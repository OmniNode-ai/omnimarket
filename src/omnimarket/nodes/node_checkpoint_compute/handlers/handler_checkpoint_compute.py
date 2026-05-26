# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCheckpointCompute — STUB.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
Ticket: OMN-12226
"""

from __future__ import annotations

from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_request import (
    ModelCheckpointRequest,
)
from omnimarket.nodes.node_checkpoint_compute.models.model_checkpoint_result import (
    ModelCheckpointResult,
)


class HandlerCheckpointCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelCheckpointRequest) -> ModelCheckpointResult:
        raise NotImplementedError(  # stub-ok
            "node_checkpoint_compute is not yet implemented (OMN-12226). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
