# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerResumeSessionCompute — Load projected session state by task_id and agent_id.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
STUB — not yet implemented (OMN-12231).
"""

from __future__ import annotations

from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_request import (
    ModelResumeSessionComputeRequest,
)
from omnimarket.nodes.node_resume_session_compute.models.model_resume_session_compute_result import (
    ModelResumeSessionComputeResult,
)


class HandlerResumeSessionCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelResumeSessionComputeRequest
    ) -> ModelResumeSessionComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_resume_session_compute is not yet implemented (OMN-12231). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
