# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerChangelogAuditCompute — STUB.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
Ticket: OMN-12225
"""

from __future__ import annotations

from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_request import (
    ModelChangelogAuditRequest,
)
from omnimarket.nodes.node_changelog_audit_compute.models.model_changelog_audit_result import (
    ModelChangelogAuditResult,
)


class HandlerChangelogAuditCompute:
    """STUB: not yet implemented. Raises NotImplementedError."""

    def handle(self, request: ModelChangelogAuditRequest) -> ModelChangelogAuditResult:
        raise NotImplementedError(  # stub-ok
            "node_changelog_audit_compute is not yet implemented (OMN-12225). "
            "Returns SkillRoutingError with reason node_not_implemented."
        )
