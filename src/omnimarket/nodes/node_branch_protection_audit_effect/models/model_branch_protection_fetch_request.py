# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read every GitHub document one audit of the policy's fleet needs."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_audit_policy import (
    ModelBranchProtectionAuditPolicy,
)
from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_audit_request import (
    DEFAULT_BRANCHES,
    DEFAULT_ORG,
)


class ModelBranchProtectionFetchRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy: ModelBranchProtectionAuditPolicy
    org: str = DEFAULT_ORG
    branches: tuple[str, ...] = DEFAULT_BRANCHES


__all__ = ["ModelBranchProtectionFetchRequest"]
