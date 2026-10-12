# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge one fetched snapshot against the audit policy."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_audit_policy import (
    ModelBranchProtectionAuditPolicy,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_snapshot import (
    ModelBranchProtectionSnapshot,
)

DEFAULT_ORG = "OmniNode-ai"
DEFAULT_BRANCHES: tuple[str, ...] = ("main", "dev")


class ModelBranchProtectionAuditRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelBranchProtectionAuditPolicy
    snapshot: ModelBranchProtectionSnapshot
    org: str = DEFAULT_ORG
    branches: tuple[str, ...] = DEFAULT_BRANCHES
    # Shown on the report's Date line; the caller supplies it so the judge
    # stays pure.
    audited_at: str = ""


__all__ = ["DEFAULT_BRANCHES", "DEFAULT_ORG", "ModelBranchProtectionAuditRequest"]
