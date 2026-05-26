# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerSkillFunctionalAuditCompute stub."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_skill_functional_audit_compute.handlers.handler_skill_functional_audit_compute import (
    HandlerSkillFunctionalAuditCompute,
)
from omnimarket.nodes.node_skill_functional_audit_compute.models.model_skill_functional_audit_compute_request import (
    ModelSkillFunctionalAuditComputeRequest,
)


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    """Stub handler must raise NotImplementedError."""
    handler = HandlerSkillFunctionalAuditCompute()
    request = ModelSkillFunctionalAuditComputeRequest()
    with pytest.raises(NotImplementedError):
        handler.handle(request)


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelSkillFunctionalAuditComputeRequest()
    with pytest.raises(ValidationError):
        request.skills_filter = ["some-skill"]  # type: ignore[misc]


@pytest.mark.unit
def test_request_default_skills_filter_is_none() -> None:
    """skills_filter defaults to None (meaning all skills)."""
    request = ModelSkillFunctionalAuditComputeRequest()
    assert request.skills_filter is None


@pytest.mark.unit
def test_request_with_explicit_filter() -> None:
    """skills_filter can be set to a list of skill names."""
    request = ModelSkillFunctionalAuditComputeRequest(
        skills_filter=["onex:aislop_sweep", "onex:contract_sweep"]
    )
    assert request.skills_filter == ["onex:aislop_sweep", "onex:contract_sweep"]
