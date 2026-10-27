# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The independent readback of admissions against limits."""

from __future__ import annotations

from omnimarket.handlers.rules_wip_gate import audit_admissions
from omnimarket.models.wip_gate import ModelWipAuditRequest, ModelWipAuditResult


class HandlerAuditAdmissions:
    """``handle(ModelWipAuditRequest) -> ModelWipAuditResult``."""

    def handle(self, request: ModelWipAuditRequest) -> ModelWipAuditResult:
        return audit_admissions(request)
