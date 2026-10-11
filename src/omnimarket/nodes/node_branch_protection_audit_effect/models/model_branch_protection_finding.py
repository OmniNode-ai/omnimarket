# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One judged line of the audit, the record the script wrote to its JSONL stream."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_branch_protection_audit_effect.models.enum_branch_protection_check_status import (
    EnumBranchProtectionCheckStatus,
)


class ModelBranchProtectionFinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    # The audited branch, or "-" for a repository-level check.
    branch: str
    check: str
    status: EnumBranchProtectionCheckStatus
    detail: str


__all__ = ["ModelBranchProtectionFinding"]
