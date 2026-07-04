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


# ---------------------------------------------------------------------------
# Markdown plan support (OMN-13923)
# ---------------------------------------------------------------------------

_ROLLING_PLAN_SHAPED_MARKDOWN = """\
# ROLLING SEVEN-DAY PLAN — canonical work driver

**This is the single driver of daily activity.** It is a *rolling* plan.

## §1 Verified current state (2026-07-03 ~15:30Z)

- Board integrity: Done-flip guard live (OMN-13801 fixed).

## §2 Work queue

- OMN-13923 plan_audit markdown support.
"""


@pytest.mark.unit
def test_handler_audits_markdown_rolling_plan_shape(tmp_path: Path) -> None:
    """Regression (OMN-13923): a Markdown plan whose bold text is invalid YAML
    (``**...`` scans as a YAML alias) must be audited as Markdown — never
    pushed through the YAML parser and never a format error."""
    plan_path = tmp_path / "ROLLING_SEVEN_DAY_PLAN.md"
    plan_path.write_text(_ROLLING_PLAN_SHAPED_MARKDOWN, encoding="utf-8")
    handler = HandlerPlanAuditCompute()
    request = ModelPlanAuditComputeRequest(plan_path=str(plan_path))

    result = handler.handle(request)

    assert result.status == "ok"
    assert result.error is None
    assert result.passed is True
    assert result.verdict == "PASS"
    assert result.violations == []
    check_names = [check.name for check in result.checks]
    assert "yaml_parse" not in check_names
    assert check_names == [
        "path",
        "markdown_structure",
        "ticket_linkage",
        "verified_state",
    ]
    assert len(result.plans) == 1
    assert result.plans[0].verdict == "PASS"


@pytest.mark.unit
def test_handler_markdown_verified_state_line_form(tmp_path: Path) -> None:
    """The 'verified: <date> via <command>' line form satisfies verified_state."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(
        "# Some Plan\n\nTicket: OMN-1234\n\n"
        "verified: 2026-07-03 via gh pr checks 42 --repo OmniNode-ai/omnimarket\n",
        encoding="utf-8",
    )
    handler = HandlerPlanAuditCompute()

    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(plan_path)))

    assert result.status == "ok"
    assert result.verdict == "PASS"
    assert result.warnings == []


@pytest.mark.unit
def test_handler_markdown_missing_verified_state_warns(tmp_path: Path) -> None:
    """A Markdown plan without a verified-state marker is WARN, not FAIL."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("# Plan\n\nDoes OMN-1 things.\n", encoding="utf-8")
    handler = HandlerPlanAuditCompute()

    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(plan_path)))

    assert result.status == "ok"
    assert result.passed is True
    assert result.verdict == "WARN"
    assert result.violations == []
    assert len(result.warnings) == 1
    assert "verified-state" in result.warnings[0]


@pytest.mark.unit
def test_handler_markdown_missing_title_and_linkage_fails(tmp_path: Path) -> None:
    """Markdown without an H1 title or any OMN-XXXX reference is FAIL."""
    plan_path = tmp_path / "plan.md"
    plan_path.write_text("just some prose\n\n## a section\n", encoding="utf-8")
    handler = HandlerPlanAuditCompute()

    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(plan_path)))

    assert result.status == "ok"
    assert result.passed is False
    assert result.verdict == "FAIL"
    assert any("H1 title" in violation for violation in result.violations)
    assert any("OMN-XXXX" in violation for violation in result.violations)


@pytest.mark.unit
def test_handler_single_unsupported_file_is_error_with_skip_entry(
    tmp_path: Path,
) -> None:
    """A single unsupported file yields an explicit error + a SKIPPED entry,
    never a vacuous pass and never a YAML crash."""
    plan_path = tmp_path / "notes.txt"
    plan_path.write_text("not a plan\n", encoding="utf-8")
    handler = HandlerPlanAuditCompute()

    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(plan_path)))

    assert result.status == "error"
    assert result.verdict == "ERROR"
    assert result.passed is False
    assert len(result.plans) == 1
    assert result.plans[0].verdict == "SKIPPED"
    assert result.plans[0].skip_reason is not None
    assert "unsupported plan format" in result.plans[0].skip_reason


@pytest.mark.unit
def test_handler_directory_mode_mixed_formats(tmp_path: Path) -> None:
    """Directory mode audits every *.md/*.yaml file and SKIPs the rest."""
    (tmp_path / "good.md").write_text(
        "# Good Plan\n\nOMN-42\n\n## Current Verified State\n\n"
        "verified: 2026-07-03 via pytest\n",
        encoding="utf-8",
    )
    (tmp_path / "good.yaml").write_text(
        "title: Plan\ntasks:\n  - id: t1\n    title: Task one\n",
        encoding="utf-8",
    )
    (tmp_path / "notes.txt").write_text("not a plan\n", encoding="utf-8")
    (tmp_path / "archive").mkdir()

    handler = HandlerPlanAuditCompute()
    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(tmp_path)))

    assert result.status == "ok"
    assert result.passed is True
    assert result.verdict == "PASS"
    by_name = {Path(entry.plan_path).name: entry for entry in result.plans}
    assert set(by_name) == {"good.md", "good.yaml", "notes.txt"}
    assert by_name["good.md"].verdict == "PASS"
    assert by_name["good.yaml"].verdict == "PASS"
    assert by_name["notes.txt"].verdict == "SKIPPED"
    assert by_name["notes.txt"].skip_reason is not None


@pytest.mark.unit
def test_handler_directory_mode_aggregates_worst_verdict(tmp_path: Path) -> None:
    """A FAIL in any file dominates the aggregate verdict; findings are
    prefixed with the file name."""
    (tmp_path / "bad.md").write_text("no title, no ticket\n", encoding="utf-8")
    (tmp_path / "warn.md").write_text("# Plan\n\nOMN-7\n", encoding="utf-8")

    handler = HandlerPlanAuditCompute()
    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(tmp_path)))

    assert result.status == "ok"
    assert result.passed is False
    assert result.verdict == "FAIL"
    assert any(violation.startswith("bad.md: ") for violation in result.violations)
    assert any(warning.startswith("warn.md: ") for warning in result.warnings)


@pytest.mark.unit
def test_handler_directory_with_zero_auditable_files_errors(tmp_path: Path) -> None:
    """Zero audited files is an error (anti-vacuous-pass), with SKIPPED
    entries preserved for visibility."""
    (tmp_path / "notes.txt").write_text("nope\n", encoding="utf-8")

    handler = HandlerPlanAuditCompute()
    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(tmp_path)))

    assert result.status == "error"
    assert result.verdict == "ERROR"
    assert result.passed is False
    assert result.error is not None
    assert "no auditable plan files" in result.error
    assert [entry.verdict for entry in result.plans] == ["SKIPPED"]


@pytest.mark.unit
def test_handler_yaml_parse_failure_is_fail_not_crash(tmp_path: Path) -> None:
    """A .yaml file with invalid YAML is a FAIL finding on that file, and the
    audit run itself still completes with status ok."""
    plan_path = tmp_path / "broken.yaml"
    plan_path.write_text("**not: [valid yaml\n", encoding="utf-8")
    handler = HandlerPlanAuditCompute()

    result = handler.handle(ModelPlanAuditComputeRequest(plan_path=str(plan_path)))

    assert result.status == "ok"
    assert result.passed is False
    assert result.verdict == "FAIL"
    assert any("failed to read or parse YAML" in v for v in result.violations)
