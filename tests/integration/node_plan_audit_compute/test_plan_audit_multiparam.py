# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter integration coverage for node_plan_audit_compute.

WS-5 Wave 8 (OMN-13682). Variant A — the COMPUTE handler is driven in-process
against synthetic plan YAML files written under ``tmp_path``. Each case varies
the plan structure (valid, missing-field, dependency cycle, unknown dependency,
non-mapping, missing-file) and asserts the typed
``ModelPlanAuditComputeResult`` fields (status, passed, per-check results,
violation messages).

Negative controls: malformed plans (cycle, unknown dep, missing field) must each
produce a recorded violation and ``passed == False``. A run that passed a
cyclic plan would be a regression.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_plan_audit_compute.handlers.handler_plan_audit_compute import (
    HandlerPlanAuditCompute,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)

_VALID_PLAN = """
title: Ship Wave 8
tasks:
  - id: t1
    title: Build node
  - id: t2
    title: Test node
    depends_on: [t1]
"""

_MISSING_TASKS = """
title: Plan with no tasks
"""

_CYCLE_PLAN = """
title: Cyclic plan
tasks:
  - id: a
    title: Task A
    depends_on: [b]
  - id: b
    title: Task B
    depends_on: [a]
"""

_UNKNOWN_DEP_PLAN = """
title: Dangling dependency
tasks:
  - id: t1
    title: Only task
    depends_on: [ghost]
"""

_NON_MAPPING_PLAN = """
- just
- a
- list
"""

# Each case: (yaml content or None for missing-file, expected_status,
#             expected_passed, violation substring or None)
_CASES = [
    pytest.param(_VALID_PLAN, "ok", True, None, id="valid-plan-passes"),
    pytest.param(
        _MISSING_TASKS,
        "ok",
        False,
        "missing required field: tasks",
        id="missing-required-field",
    ),
    pytest.param(
        _CYCLE_PLAN,
        "ok",
        False,
        "dependency cycle detected",
        id="dependency-cycle-negative-control",
    ),
    pytest.param(
        _UNKNOWN_DEP_PLAN,
        "ok",
        False,
        "depends on unknown task: ghost",
        id="unknown-dependency-negative-control",
    ),
    pytest.param(
        _NON_MAPPING_PLAN,
        "ok",
        False,
        "top-level mapping",
        id="non-mapping-yaml",
    ),
    pytest.param(
        None,
        "error",
        False,
        "does not reference a file",
        id="missing-file-error",
    ),
]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("content", "expected_status", "expected_passed", "violation_substr"), _CASES
)
def test_plan_audit_multiparam(
    tmp_path: Path,
    content: str | None,
    expected_status: str,
    expected_passed: bool,
    violation_substr: str | None,
) -> None:
    if content is None:
        plan_path = tmp_path / "does_not_exist.yaml"
    else:
        plan_path = tmp_path / "plan.yaml"
        plan_path.write_text(content, encoding="utf-8")

    result = HandlerPlanAuditCompute().handle(
        ModelPlanAuditComputeRequest(plan_path=str(plan_path))
    )

    assert result.status == expected_status
    assert result.passed is expected_passed
    assert result.checks, "audit must record at least one check result"

    if expected_passed:
        assert result.violations == []
        assert all(check.passed for check in result.checks)
    else:
        assert result.violations, "a failing plan must record a violation"
        assert violation_substr is not None
        joined = " | ".join(result.violations)
        assert violation_substr in joined, joined
        # At least one check must be marked failed for a non-passing plan.
        assert any(not check.passed for check in result.checks)


@pytest.mark.integration
def test_plan_audit_rejects_relative_path(tmp_path: Path) -> None:
    """A relative plan_path is rejected before any file read."""
    result = HandlerPlanAuditCompute().handle(
        ModelPlanAuditComputeRequest(plan_path="relative/plan.yaml")
    )
    assert result.status == "error"
    assert result.passed is False
    assert result.error is not None
    assert "absolute" in result.error
