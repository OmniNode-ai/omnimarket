# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerSkillFunctionalAuditCompute — Stub detection and connectivity checks across skills.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
STUB — not yet implemented (OMN-12231).
"""

from __future__ import annotations

from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_result import (
    ModelSkillFunctionalAuditComputeResult,
)


class HandlerSkillFunctionalAuditCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelSkillFunctionalAuditComputeRequest
    ) -> ModelSkillFunctionalAuditComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_skill_functional_audit_compute is not yet implemented (OMN-12231). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
