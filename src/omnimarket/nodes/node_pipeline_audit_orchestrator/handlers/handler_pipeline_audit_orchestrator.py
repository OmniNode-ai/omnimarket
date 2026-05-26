# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler stub for node_pipeline_audit_orchestrator [OMN-12211].

ORCHESTRATOR node. Consumes ModelPipelineAuditRequest, dispatches parallel
audit agents per repo (Phases 1-4 of the pipeline_audit skill algorithm),
aggregates per-repo findings, classifies by severity, and emits a
ModelPipelineAuditResult containing a severity-ordered gap register.

Implementation is deferred (node_not_implemented: true). This stub raises
NotImplementedError so the runtime fails loudly rather than silently misbehaving.
"""

from __future__ import annotations

from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_request import (
    ModelPipelineAuditRequest,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_result import (
    ModelPipelineAuditResult,
)


class HandlerPipelineAuditOrchestrator:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelPipelineAuditRequest) -> ModelPipelineAuditResult:
        raise NotImplementedError(  # stub-ok
            "node_pipeline_audit_orchestrator is not yet implemented (OMN-12211). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
