# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerPlanAuditCompute stub."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_plan_audit_compute.handlers.handler_plan_audit_compute import (
    HandlerPlanAuditCompute,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    """Stub handler must raise NotImplementedError."""
    handler = HandlerPlanAuditCompute()
    request = ModelPlanAuditComputeRequest(plan_path="/tmp/plan.yaml")
    with pytest.raises(NotImplementedError):
        handler.handle(request)


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelPlanAuditComputeRequest(plan_path="/tmp/plan.yaml")
    with pytest.raises(ValidationError):
        request.plan_path = "/other/plan.yaml"  # type: ignore[misc]
