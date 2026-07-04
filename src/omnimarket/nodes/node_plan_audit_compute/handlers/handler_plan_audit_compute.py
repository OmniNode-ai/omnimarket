# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPlanAuditCompute — plan-file audit for YAML and Markdown plans.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.

Supported-format contract (OMN-13923):

- ``*.yaml`` / ``*.yml`` — structured plan schema: top-level mapping with a
  non-empty ``title`` and a ``tasks`` list (id/title per task, optional
  ``dependencies``/``depends_on``), plus dependency-cycle detection.
- ``*.md`` / ``*.markdown`` — markdown plan template: H1 title heading,
  Linear ticket linkage (``OMN-XXXX``), and a verified-state marker
  (a ``verified: <date> via <command>`` line or a heading containing
  "verified ... state" — the Current-Verified-State gate contract).
  A missing verified-state marker is advisory (WARN), not a violation.
- any other extension — reported as SKIPPED with a reason, never an error.

``plan_path`` may also point at a directory, in which case every regular file
directly inside it is audited (or SKIPPED); a run that audits zero files is an
error, never a vacuous pass.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, TypeGuard

import yaml

from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_result import (
    EnumPlanAuditVerdict,
    ModelCheckResult,
    ModelPlanAuditComputeResult,
    ModelPlanFileAuditResult,
)

_REQUIRED_PLAN_FIELDS = ("title", "tasks")
_REQUIRED_TASK_FIELDS = ("id", "title")

_YAML_SUFFIXES = (".yaml", ".yml")
_MARKDOWN_SUFFIXES = (".md", ".markdown")
_SUPPORTED_SUFFIXES = _MARKDOWN_SUFFIXES + _YAML_SUFFIXES

# H1 title heading: "# <text>" on its own line.
_H1_RE = re.compile(r"^#\s+\S", re.MULTILINE)

# Linear ticket linkage anywhere in the document.
_TICKET_RE = re.compile(r"\bOMN-\d+\b")

# Verified-state marker, per the Current-Verified-State gate contract:
# either a "verified: <YYYY-MM-DD> via <command>" line ...
_VERIFIED_LINE_RE = re.compile(
    r"^\s*verified:\s*\d{4}-\d{2}-\d{2}\s+via\s+\S",
    re.IGNORECASE | re.MULTILINE,
)
# ... or a heading whose text mentions verified + state in either order
# ("## Current Verified State", "## §1 Verified current state (…)").
_VERIFIED_HEADING_RE = re.compile(
    r"^#{1,6}\s+[^\n]*\bverified\b[^\n]*\bstate\b"
    r"|^#{1,6}\s+[^\n]*\bstate\b[^\n]*\bverified\b",
    re.IGNORECASE | re.MULTILINE,
)


class HandlerPlanAuditCompute:
    """Deterministic auditor for YAML and Markdown implementation plans."""

    def handle(
        self, request: ModelPlanAuditComputeRequest
    ) -> ModelPlanAuditComputeResult:
        plan_path = Path(request.plan_path)

        if not plan_path.is_absolute():
            return self._error_result("path", "plan_path must be absolute")

        if plan_path.is_dir():
            return self._handle_directory(plan_path)

        if not plan_path.is_file():
            detail = f"plan_path does not reference a file: {request.plan_path}"
            return self._error_result("path", detail)

        entry = self._audit_file(plan_path)
        if entry.verdict is EnumPlanAuditVerdict.SKIPPED:
            detail = (
                f"no auditable plan file: {entry.skip_reason}"
                if entry.skip_reason
                else "no auditable plan file"
            )
            return self._error_result("format", detail, plans=[entry])

        checks = [ModelCheckResult(name="path", passed=True), *entry.checks]
        return self._aggregate_result(checks, [entry])

    def _handle_directory(self, plan_dir: Path) -> ModelPlanAuditComputeResult:
        files = sorted(path for path in plan_dir.iterdir() if path.is_file())
        entries = [self._audit_file(path) for path in files]
        audited = [
            entry
            for entry in entries
            if entry.verdict is not EnumPlanAuditVerdict.SKIPPED
        ]

        if not audited:
            detail = (
                "no auditable plan files in directory "
                f"(supported: {', '.join(_SUPPORTED_SUFFIXES)}): {plan_dir}"
            )
            return self._error_result("path", detail, plans=entries)

        checks = [ModelCheckResult(name="path", passed=True)]
        for entry in entries:
            name = Path(entry.plan_path).name
            if entry.verdict is EnumPlanAuditVerdict.SKIPPED:
                detail = f"SKIPPED: {entry.skip_reason}"
            else:
                findings = "; ".join([*entry.violations, *entry.warnings])
                detail = entry.verdict.value + (f": {findings}" if findings else "")
            checks.append(
                ModelCheckResult(
                    name=name,
                    passed=entry.verdict is not EnumPlanAuditVerdict.FAIL,
                    detail=detail,
                )
            )
        return self._aggregate_result(checks, entries, prefix_findings=True)

    # ------------------------------------------------------------------
    # Per-file audits
    # ------------------------------------------------------------------

    def _audit_file(self, plan_path: Path) -> ModelPlanFileAuditResult:
        suffix = plan_path.suffix.lower()
        if suffix in _YAML_SUFFIXES:
            return self._audit_yaml(plan_path)
        if suffix in _MARKDOWN_SUFFIXES:
            return self._audit_markdown(plan_path)
        return ModelPlanFileAuditResult(
            plan_path=str(plan_path),
            verdict=EnumPlanAuditVerdict.SKIPPED,
            skip_reason=(
                f"unsupported plan format '{suffix or plan_path.name}' "
                f"(supported: {', '.join(_SUPPORTED_SUFFIXES)})"
            ),
        )

    def _audit_yaml(self, plan_path: Path) -> ModelPlanFileAuditResult:
        checks: list[ModelCheckResult] = []
        violations: list[str] = []

        try:
            # fmt: off
            raw_plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))  # node-purity-ok: OMN-9048
            # fmt: on
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            detail = f"failed to read or parse YAML: {exc}"
            checks.append(
                ModelCheckResult(name="yaml_parse", passed=False, detail=detail)
            )
            violations.append(detail)
            return self._file_result(plan_path, checks, violations, [])

        checks.append(ModelCheckResult(name="yaml_parse", passed=True))

        if not isinstance(raw_plan, dict):
            detail = "plan YAML must parse to a top-level mapping"
            checks.append(
                ModelCheckResult(
                    name="top_level_mapping",
                    passed=False,
                    detail=detail,
                )
            )
            violations.append(detail)
            return self._file_result(plan_path, checks, violations, [])

        checks.append(ModelCheckResult(name="top_level_mapping", passed=True))

        plan = dict(raw_plan)
        self._check_required_plan_fields(plan, checks, violations)
        task_graph = self._check_tasks(plan.get("tasks"), checks, violations)
        self._check_dependency_cycles(task_graph, checks, violations)

        return self._file_result(plan_path, checks, violations, [])

    def _audit_markdown(self, plan_path: Path) -> ModelPlanFileAuditResult:
        checks: list[ModelCheckResult] = []
        violations: list[str] = []
        warnings: list[str] = []

        try:
            # fmt: off
            text = plan_path.read_text(encoding="utf-8")  # node-purity-ok: OMN-9048
            # fmt: on
        except (OSError, UnicodeDecodeError) as exc:
            detail = f"failed to read markdown plan: {exc}"
            checks.append(ModelCheckResult(name="read", passed=False, detail=detail))
            violations.append(detail)
            return self._file_result(plan_path, checks, violations, warnings)

        structure_issues: list[str] = []
        if not text.strip():
            structure_issues.append("markdown plan is empty")
        elif not _H1_RE.search(text):
            structure_issues.append("markdown plan has no H1 title heading ('# …')")
        detail = "; ".join(structure_issues)
        checks.append(
            ModelCheckResult(
                name="markdown_structure",
                passed=not structure_issues,
                detail=detail,
            )
        )
        violations.extend(structure_issues)

        has_ticket = bool(_TICKET_RE.search(text))
        ticket_detail = "" if has_ticket else "no Linear ticket reference (OMN-XXXX)"
        checks.append(
            ModelCheckResult(
                name="ticket_linkage",
                passed=has_ticket,
                detail=ticket_detail,
            )
        )
        if not has_ticket:
            violations.append(ticket_detail)

        has_verified_state = bool(
            _VERIFIED_LINE_RE.search(text) or _VERIFIED_HEADING_RE.search(text)
        )
        verified_detail = (
            ""
            if has_verified_state
            else (
                "no verified-state marker (expected a 'verified: <date> via "
                "<command>' line or a verified-state heading)"
            )
        )
        checks.append(
            ModelCheckResult(
                name="verified_state",
                passed=has_verified_state,
                detail=verified_detail,
            )
        )
        if not has_verified_state:
            warnings.append(verified_detail)

        return self._file_result(plan_path, checks, violations, warnings)

    # ------------------------------------------------------------------
    # Result assembly
    # ------------------------------------------------------------------

    def _file_result(
        self,
        plan_path: Path,
        checks: list[ModelCheckResult],
        violations: list[str],
        warnings: list[str],
    ) -> ModelPlanFileAuditResult:
        if violations:
            verdict = EnumPlanAuditVerdict.FAIL
        elif warnings:
            verdict = EnumPlanAuditVerdict.WARN
        else:
            verdict = EnumPlanAuditVerdict.PASS
        return ModelPlanFileAuditResult(
            plan_path=str(plan_path),
            verdict=verdict,
            checks=checks,
            violations=violations,
            warnings=warnings,
        )

    def _aggregate_result(
        self,
        checks: list[ModelCheckResult],
        entries: list[ModelPlanFileAuditResult],
        prefix_findings: bool = False,
    ) -> ModelPlanAuditComputeResult:
        violations: list[str] = []
        warnings: list[str] = []
        for entry in entries:
            prefix = f"{Path(entry.plan_path).name}: " if prefix_findings else ""
            violations.extend(f"{prefix}{item}" for item in entry.violations)
            warnings.extend(f"{prefix}{item}" for item in entry.warnings)

        verdicts = {entry.verdict for entry in entries}
        if EnumPlanAuditVerdict.FAIL in verdicts:
            verdict = EnumPlanAuditVerdict.FAIL
        elif EnumPlanAuditVerdict.WARN in verdicts:
            verdict = EnumPlanAuditVerdict.WARN
        else:
            verdict = EnumPlanAuditVerdict.PASS

        return ModelPlanAuditComputeResult(
            status="ok",
            passed=not violations,
            verdict=verdict,
            checks=checks,
            violations=violations,
            warnings=warnings,
            plans=entries,
        )

    def _error_result(
        self,
        check_name: str,
        detail: str,
        plans: list[ModelPlanFileAuditResult] | None = None,
    ) -> ModelPlanAuditComputeResult:
        return ModelPlanAuditComputeResult(
            status="error",
            passed=False,
            verdict=EnumPlanAuditVerdict.ERROR,
            checks=[ModelCheckResult(name=check_name, passed=False, detail=detail)],
            violations=[detail],
            plans=plans or [],
            error=detail,
        )

    # ------------------------------------------------------------------
    # YAML schema checks
    # ------------------------------------------------------------------

    def _check_required_plan_fields(
        self,
        plan: dict[Any, Any],
        checks: list[ModelCheckResult],
        violations: list[str],
    ) -> None:
        missing = [field for field in _REQUIRED_PLAN_FIELDS if field not in plan]
        invalid: list[str] = []
        if "title" in plan and not _is_non_empty_str(plan["title"]):
            invalid.append("title must be a non-empty string")
        if "tasks" in plan and not isinstance(plan["tasks"], list):
            invalid.append("tasks must be a list")

        details = [f"missing required field: {field}" for field in missing]
        details.extend(invalid)
        detail = "; ".join(details)
        checks.append(
            ModelCheckResult(
                name="required_fields",
                passed=not details,
                detail=detail,
            )
        )
        violations.extend(details)

    def _check_tasks(
        self,
        raw_tasks: Any,
        checks: list[ModelCheckResult],
        violations: list[str],
    ) -> dict[str, list[str]]:
        task_graph: dict[str, list[str]] = {}
        details: list[str] = []

        if not isinstance(raw_tasks, list):
            checks.append(
                ModelCheckResult(
                    name="task_schema",
                    passed=False,
                    detail="tasks must be a list",
                )
            )
            return task_graph

        if not raw_tasks:
            details.append("tasks must contain at least one task")

        seen_ids: set[str] = set()
        pending_dependencies: dict[str, list[str]] = {}

        for index, raw_task in enumerate(raw_tasks):
            task_label = f"tasks[{index}]"
            if not isinstance(raw_task, dict):
                details.append(f"{task_label} must be a mapping")
                continue

            missing = [
                field for field in _REQUIRED_TASK_FIELDS if field not in raw_task
            ]
            details.extend(
                f"{task_label} missing required field: {field}" for field in missing
            )

            task_id_value = raw_task.get("id")
            if not _is_non_empty_str(task_id_value):
                details.append(f"{task_label}.id must be a non-empty string")
                continue
            task_id = task_id_value

            if task_id in seen_ids:
                details.append(f"duplicate task id: {task_id}")
            seen_ids.add(task_id)

            if "title" in raw_task and not _is_non_empty_str(raw_task["title"]):
                details.append(f"task {task_id}.title must be a non-empty string")

            dependencies = _dependencies_for(raw_task)
            if dependencies is None:
                details.append(f"task {task_id} dependencies must be a list of strings")
                dependencies = []

            pending_dependencies[task_id] = dependencies
            task_graph[task_id] = dependencies

        unknown_dependencies = [
            f"task {task_id} depends on unknown task: {dependency}"
            for task_id, dependencies in pending_dependencies.items()
            for dependency in dependencies
            if dependency not in seen_ids
        ]
        details.extend(unknown_dependencies)

        detail = "; ".join(details)
        checks.append(
            ModelCheckResult(
                name="task_schema",
                passed=not details,
                detail=detail,
            )
        )
        violations.extend(details)
        return task_graph

    def _check_dependency_cycles(
        self,
        task_graph: dict[str, list[str]],
        checks: list[ModelCheckResult],
        violations: list[str],
    ) -> None:
        cycle = _find_cycle(task_graph)
        detail = ""
        if cycle:
            detail = "dependency cycle detected: " + " -> ".join(cycle)
            violations.append(detail)

        checks.append(
            ModelCheckResult(
                name="dependency_cycles",
                passed=cycle is None,
                detail=detail,
            )
        )


def _dependencies_for(task: dict[Any, Any]) -> list[str] | None:
    dependencies = task.get("dependencies", task.get("depends_on", []))
    if dependencies is None:
        return []
    if not isinstance(dependencies, list):
        return None
    if any(not isinstance(dependency, str) for dependency in dependencies):
        return None
    return dependencies


def _find_cycle(task_graph: dict[str, list[str]]) -> list[str] | None:
    visiting: list[str] = []
    visited: set[str] = set()

    def visit(task_id: str) -> list[str] | None:
        if task_id in visiting:
            start = visiting.index(task_id)
            return [*visiting[start:], task_id]
        if task_id in visited:
            return None

        visiting.append(task_id)
        for dependency in task_graph.get(task_id, []):
            if dependency not in task_graph:
                continue
            cycle = visit(dependency)
            if cycle:
                return cycle
        visiting.pop()
        visited.add(task_id)
        return None

    for task_id in task_graph:
        cycle = visit(task_id)
        if cycle:
            return cycle
    return None


def _is_non_empty_str(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and bool(value.strip())
