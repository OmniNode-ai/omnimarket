# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPlanAuditCompute — YAML validation, field checks, cycle detection for plan files.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
STUB — not yet implemented (OMN-12231).
"""

from __future__ import annotations

from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_result import (
    ModelPlanAuditComputeResult,
)


class HandlerPlanAuditCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(
        self, request: ModelPlanAuditComputeRequest
    ) -> ModelPlanAuditComputeResult:
        raise NotImplementedError(  # stub-ok
            "node_plan_audit_compute is not yet implemented (OMN-12231). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
