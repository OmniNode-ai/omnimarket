# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A node directory without an onex.nodes entry point must fail the required lint job on every PR.

The ``lint`` job in ``.github/workflows/ci.yml`` carries a step named
``Node directory has its onex.nodes entry point`` that runs the inventory
falsifier for a node directory that lost its ``onex.nodes`` entry point. This
file proves that step is wired so the guard cannot be skipped: the job exists,
is not ``continue-on-error``, is not gated by a condition that can skip it
other than ``always()``, the step exists and is not conditional or
``continue-on-error``, the step runs the target test with the target selector,
and the job is a strict CI Summary gate. A failing control per break confirms
the guard catches each one.
"""

from __future__ import annotations

import ast
import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

STEP_NAME = "Node directory has its onex.nodes entry point"
TARGET_TEST = "tests/test_market_node_runtime_dogfood.py"
SELECTOR = "inventory_fails_on_a_missing_or_extra_node and _without_entry_point"


def _parse_workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def guard_violations(workflow: dict[str, Any]) -> list[str]:
    """Every way the node-entry-point guard step can be missing or weakened.

    Returns an empty list when the guard is correctly wired, and one message
    per violation otherwise.
    """
    violations: list[str] = []
    jobs: dict[str, Any] = workflow.get("jobs", {})
    if "lint" not in jobs:
        return ["the lint job is missing from the workflow"]
    lint: dict[str, Any] = jobs["lint"]
    if lint.get("continue-on-error") is True:
        violations.append("the lint job is continue-on-error, so it cannot fail a PR")
    if lint.get("if") not in (None, "always()"):
        violations.append(f"the lint job can be skipped by {lint.get('if')!r}")
    steps: list[dict[str, Any]] = lint.get("steps", [])
    step = next((s for s in steps if s.get("name") == STEP_NAME), None)
    if step is None:
        violations.append(f"no step named {STEP_NAME!r} in the lint job")
        return violations
    if step.get("if") is not None:
        violations.append(f"the guard step can be skipped by {step.get('if')!r}")
    if step.get("continue-on-error") is True:
        violations.append("the guard step is continue-on-error, so it cannot fail a PR")
    run = str(step.get("run", ""))
    if TARGET_TEST not in run:
        violations.append(f"the guard step does not run {TARGET_TEST!r}")
    if SELECTOR not in run:
        violations.append(f"the guard step does not select {SELECTOR!r}")
    lint_name = str(lint.get("name", "lint"))
    if lint_name not in STRICT_GATE_JOBS:
        violations.append(
            f"{lint_name!r} is not in STRICT_GATE_JOBS, so a skipped or failed "
            "guard run would not block the required CI Summary context"
        )
    return violations


def test_real_workflow_is_clean() -> None:
    assert guard_violations(_parse_workflow()) == []


def _remove_step(workflow: dict[str, Any]) -> dict[str, Any]:
    wf = copy.deepcopy(workflow)
    steps = wf["jobs"]["lint"]["steps"]
    wf["jobs"]["lint"]["steps"] = [s for s in steps if s.get("name") != STEP_NAME]
    return wf


def _step_if_merge_group(workflow: dict[str, Any]) -> dict[str, Any]:
    wf = copy.deepcopy(workflow)
    for step in wf["jobs"]["lint"]["steps"]:
        if step.get("name") == STEP_NAME:
            step["if"] = "github.event_name == 'merge_group'"
    return wf


def _step_continue_on_error(workflow: dict[str, Any]) -> dict[str, Any]:
    wf = copy.deepcopy(workflow)
    for step in wf["jobs"]["lint"]["steps"]:
        if step.get("name") == STEP_NAME:
            step["continue-on-error"] = True
    return wf


def _step_run_no_match(workflow: dict[str, Any]) -> dict[str, Any]:
    wf = copy.deepcopy(workflow)
    for step in wf["jobs"]["lint"]["steps"]:
        if step.get("name") == STEP_NAME:
            step["run"] = str(step["run"]).replace(SELECTOR, "nothing_matches")
    return wf


def _lint_continue_on_error(workflow: dict[str, Any]) -> dict[str, Any]:
    wf = copy.deepcopy(workflow)
    wf["jobs"]["lint"]["continue-on-error"] = True
    return wf


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_remove_step, id="step-removed"),
        pytest.param(_step_if_merge_group, id="step-if-merge-group"),
        pytest.param(_step_continue_on_error, id="step-continue-on-error"),
        pytest.param(_step_run_no_match, id="step-run-no-match"),
        pytest.param(_lint_continue_on_error, id="lint-continue-on-error"),
    ],
)
def test_failing_controls_are_caught(
    mutate: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    assert guard_violations(mutate(_parse_workflow())) != []


def test_selector_reality_by_ast() -> None:
    source = (REPO_ROOT / TARGET_TEST).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    by_name: dict[str, ast.FunctionDef] = {f.name: f for f in functions}

    test_fn = by_name["test_market_node_inventory_fails_on_a_missing_or_extra_node"]
    parametrize_ids: list[str] = []
    for decorator in test_fn.decorator_list:
        if (
            isinstance(decorator, ast.Call)
            and getattr(decorator.func, "attr", None) == "parametrize"
        ):
            for arg in decorator.args:
                if isinstance(arg, ast.List):
                    for elt in arg.elts:
                        if isinstance(elt, ast.Name):
                            parametrize_ids.append(elt.id)
    assert "_without_entry_point" in parametrize_ids, (
        f"the inventory falsifier must parametrize _without_entry_point; got {parametrize_ids}"
    )

    without = by_name["_without_entry_point"]
    without_source = ast.unparse(without)
    assert "del entry_points[" in without_source, (
        "_without_entry_point must delete an entry point to simulate a missing one"
    )

    body = test_fn.body
    assert body, "the inventory falsifier has no body"
    first = body[0]
    assert isinstance(first, ast.Assert), (
        f"the falsifier's first statement must be an assert, got {type(first).__name__}"
    )
    unparse = ast.unparse(first)
    assert "_inventory_violations(_real_inventory()) == []" in unparse, (
        f"the falsifier must assert a clean real inventory first; got {unparse!r}"
    )
