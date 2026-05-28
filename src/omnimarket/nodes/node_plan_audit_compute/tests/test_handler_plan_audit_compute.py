# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for HandlerPlanAuditCompute."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_plan_audit_compute.handlers.handler_plan_audit_compute import (
    HandlerPlanAuditCompute,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)


@pytest.mark.unit
def test_handler_audits_valid_plan(tmp_path: Path) -> None:
    """Valid YAML plans pass deterministic schema and dependency checks."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        """
title: Release hardening
tasks:
  - id: task-a
    title: Add contract
  - id: task-b
    title: Prove behavior
    dependencies:
      - task-a
""".strip(),
        encoding="utf-8",
    )
    handler = HandlerPlanAuditCompute()
    request = ModelPlanAuditComputeRequest(plan_path=str(plan_path))

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.passed is True
    assert result.violations == []
    assert [check.name for check in result.checks] == [
        "path",
        "yaml_parse",
        "top_level_mapping",
        "required_fields",
        "task_schema",
        "dependency_cycles",
    ]


@pytest.mark.unit
def test_handler_reports_schema_violations(tmp_path: Path) -> None:
    """Missing fields and unknown dependencies return failed checks."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        """
title: ""
tasks:
  - id: task-a
    dependencies:
      - missing-task
""".strip(),
        encoding="utf-8",
    )
    handler = HandlerPlanAuditCompute()
    request = ModelPlanAuditComputeRequest(plan_path=str(plan_path))

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.passed is False
    assert "title must be a non-empty string" in result.violations
    assert "tasks[0] missing required field: title" in result.violations
    assert "task task-a depends on unknown task: missing-task" in result.violations


@pytest.mark.unit
def test_handler_reports_dependency_cycle(tmp_path: Path) -> None:
    """Task dependency cycles fail the cycle check."""
    plan_path = tmp_path / "plan.yaml"
    plan_path.write_text(
        """
title: Cyclic plan
tasks:
  - id: task-a
    title: First
    depends_on:
      - task-b
  - id: task-b
    title: Second
    depends_on:
      - task-a
""".strip(),
        encoding="utf-8",
    )
    handler = HandlerPlanAuditCompute()
    request = ModelPlanAuditComputeRequest(plan_path=str(plan_path))

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.passed is False
    assert "dependency cycle detected: task-a -> task-b -> task-a" in result.violations


@pytest.mark.unit
def test_handler_rejects_relative_plan_path() -> None:
    """The contract requires an absolute plan_path."""
    handler = HandlerPlanAuditCompute()
    request = ModelPlanAuditComputeRequest(plan_path="plan.yaml")

    result = handler.handle(request)

    assert result.status == "error"
    assert result.passed is False
    assert result.error == "plan_path must be absolute"


@pytest.mark.unit
def test_request_model_frozen() -> None:
    """Request model must be frozen (immutable)."""
    request = ModelPlanAuditComputeRequest(plan_path="/tmp/plan.yaml")
    with pytest.raises(ValidationError):
        request.plan_path = "/other/plan.yaml"  # type: ignore[misc]
