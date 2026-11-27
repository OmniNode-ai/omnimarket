# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lint job is split into parallel group jobs behind one ``lint`` aggregate (OMN-24562).

One ``lint`` job ran about 128 gate steps one after another and took 20 to 30 minutes on every
pull request. The steps now run in ``lint-group-<n>`` jobs side by side and the required ``lint``
context is an aggregate of them, so branch protection and CI Summary still read one context named
``lint``. ``lint_steps_before_omn24562.txt`` lists every step the single job carried; a step missing
from the groups fails here.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.ci_summary_gate import STRICT_GATE_JOBS
from tests.ci.lint_jobs import LINT_AGGREGATE, lint_group_ids

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
BEFORE = Path(__file__).with_name("lint_steps_before_omn24562.txt")
SETUP_STEPS = [
    "Clone omnibase_core",
    "Clone omnimemory",
    "Install uv",
    "Set up Python 3.12",
    "Install dependencies",
]
MIN_GROUPS = 4


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _before() -> list[str]:
    return [
        line
        for line in BEFORE.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    ]


def missing_lint_steps(workflow: dict[str, Any], before: list[str]) -> list[str]:
    """Steps of the old single lint job that no group job carries."""
    jobs = workflow["jobs"]
    carried: set[str] = set()
    for group in lint_group_ids(jobs):
        carried.update(str(step.get("name")) for step in jobs[group]["steps"])
    return [name for name in before if name not in carried]


def duplicated_lint_steps(workflow: dict[str, Any]) -> list[str]:
    """Check steps carried by more than one group (run twice, wasting the split)."""
    jobs = workflow["jobs"]
    seen: dict[str, int] = {}
    for group in lint_group_ids(jobs):
        for step in jobs[group]["steps"][len(SETUP_STEPS) + 1 :]:
            name = str(step.get("name"))
            seen[name] = seen.get(name, 0) + 1
    return sorted(name for name, count in seen.items() if count > 1)


def test_before_list_is_the_recorded_128_steps() -> None:
    names = _before()
    assert len(names) == 128
    assert len(set(names)) == 128


def test_no_lint_step_is_missing_from_the_groups() -> None:
    assert missing_lint_steps(_workflow(), _before()) == []


def test_no_lint_step_runs_in_two_groups() -> None:
    assert duplicated_lint_steps(_workflow()) == []


def test_dropping_a_step_is_refused() -> None:
    workflow = copy.deepcopy(_workflow())
    jobs = workflow["jobs"]
    victim = "Ruff check"
    for group in lint_group_ids(jobs):
        jobs[group]["steps"] = [
            s for s in jobs[group]["steps"] if s.get("name") != victim
        ]
    assert missing_lint_steps(workflow, _before()) == [victim]


def test_there_are_parallel_groups() -> None:
    assert len(lint_group_ids(_workflow()["jobs"])) >= MIN_GROUPS


def test_each_group_is_set_up_and_cannot_be_skipped() -> None:
    jobs = _workflow()["jobs"]
    for group in lint_group_ids(jobs):
        job = jobs[group]
        names = [str(s.get("name")) for s in job["steps"]]
        assert names[1:6] == SETUP_STEPS, group
        assert job["needs"] == "repo-evidence-preflight", group
        assert job["if"] == "always()", group
        assert not job.get("continue-on-error", False), group
        assert "name" not in job, f"{group} must keep its job id as its context name"
        assert len(names) > len(SETUP_STEPS) + 1, group


def test_aggregate_keeps_the_required_context_name_and_waits_for_every_group() -> None:
    jobs = _workflow()["jobs"]
    aggregate = jobs[LINT_AGGREGATE]
    assert "name" not in aggregate, "the job id is the required context 'lint'"
    assert sorted(aggregate["needs"]) == sorted(lint_group_ids(jobs))
    assert aggregate["if"] == "always()"
    assert not aggregate.get("continue-on-error", False)
    env: dict[str, str] = {}
    for step in aggregate["steps"]:
        env.update(step.get("env", {}))
    for group in lint_group_ids(jobs):
        assert f"needs.{group}.result" in " ".join(env.values()), group


def test_aggregate_is_still_a_strict_ci_summary_gate() -> None:
    assert LINT_AGGREGATE in STRICT_GATE_JOBS
