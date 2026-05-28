# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPlanAuditCompute — YAML validation, field checks, cycle detection for plan files.

ONEX node type: COMPUTE — pure, deterministic, no LLM calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeGuard

import yaml

from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_request import (
    ModelPlanAuditComputeRequest,
)
from omnimarket.nodes.node_plan_audit_compute.models.model_plan_audit_compute_result import (
    ModelCheckResult,
    ModelPlanAuditComputeResult,
)

_REQUIRED_PLAN_FIELDS = ("title", "tasks")
_REQUIRED_TASK_FIELDS = ("id", "title")


class HandlerPlanAuditCompute:
    """Deterministic auditor for structured YAML implementation plans."""

    def handle(
        self, request: ModelPlanAuditComputeRequest
    ) -> ModelPlanAuditComputeResult:
        checks: list[ModelCheckResult] = []
        violations: list[str] = []
        plan_path = Path(request.plan_path)

        if not plan_path.is_absolute():
            detail = "plan_path must be absolute"
            return self._error_result("path", detail, checks, violations)

        if not plan_path.is_file():
            detail = f"plan_path does not reference a file: {request.plan_path}"
            return self._error_result("path", detail, checks, violations)

        checks.append(ModelCheckResult(name="path", passed=True))

        try:
            raw_plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            return self._error_result(
                "yaml_parse",
                f"failed to read or parse YAML: {exc}",
                checks,
                violations,
            )

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
            return self._completed_result(checks, violations)

        checks.append(ModelCheckResult(name="top_level_mapping", passed=True))

        plan = dict(raw_plan)
        self._check_required_plan_fields(plan, checks, violations)
        task_graph = self._check_tasks(plan.get("tasks"), checks, violations)
        self._check_dependency_cycles(task_graph, checks, violations)

        return self._completed_result(checks, violations)

    def _error_result(
        self,
        check_name: str,
        detail: str,
        checks: list[ModelCheckResult],
        violations: list[str],
    ) -> ModelPlanAuditComputeResult:
        checks.append(ModelCheckResult(name=check_name, passed=False, detail=detail))
        violations.append(detail)
        return ModelPlanAuditComputeResult(
            status="error",
            passed=False,
            checks=checks,
            violations=violations,
            error=detail,
        )

    def _completed_result(
        self, checks: list[ModelCheckResult], violations: list[str]
    ) -> ModelPlanAuditComputeResult:
        return ModelPlanAuditComputeResult(
            status="ok",
            passed=not violations,
            checks=checks,
            violations=violations,
        )

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
