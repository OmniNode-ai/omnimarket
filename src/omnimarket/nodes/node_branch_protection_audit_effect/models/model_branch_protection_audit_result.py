# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The audit verdict: exit status, counts, findings and the text report."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_branch_protection_audit_effect.models.model_branch_protection_finding import (
    ModelBranchProtectionFinding,
)


class ModelBranchProtectionAuditResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # 0 when every audited repository is compliant, 1 on any deviation.
    exit_code: int
    failures: int
    total_checks: int
    findings: tuple[ModelBranchProtectionFinding, ...]
    noncompliant_repos: tuple[str, ...]
    report: str


__all__ = ["ModelBranchProtectionAuditResult"]
