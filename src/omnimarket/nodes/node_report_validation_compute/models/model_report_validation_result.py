# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for node_report_validation_compute (OMN-15163)."""

from __future__ import annotations

from uuid import UUID

from omnibase_core.enums.enum_dispatch_report_role import EnumDispatchReportRole
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_report_validation_compute.models.model_dispatch_worker_role import (
    EnumDispatchWorkerRole,
)
from omnimarket.nodes.node_report_validation_compute.models.model_report_validation_verdict import (
    EnumReportValidationVerdict,
)


class ModelReportValidationResult(BaseModel):
    """Deterministic verdict for one raw dispatch-report validation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    dispatch_role: EnumDispatchWorkerRole
    report_role: EnumDispatchReportRole | None = Field(
        default=None,
        description=(
            "The resolved omnibase_core report role, or None when "
            "dispatch_role has no ROLE_MAPPING_TABLE entry (declared "
            "out-of-scope, e.g. 'ops' -- see model_role_mapping.py)."
        ),
    )
    verdict: EnumReportValidationVerdict
    violations: tuple[str, ...] = Field(
        default=(),
        description=(
            "One entry per violating field/check, empty iff verdict is "
            "VALID. Each entry is prefixed by the failing check class "
            "(shape: / anchor_missing_context: / anchor_unresolved:) so a "
            "caller can classify without re-deriving the verdict."
        ),
    )


__all__ = ["ModelReportValidationResult"]
