# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22946: a fresh pull request event starts no reviewer job on a lab runner.

Rebuild plan section 8, R3 (RULING 2026-11-05T20:27:56Z): the reviewer triggers
were removed instead of moved, so no reviewer job reaches a lab runner when a
head updates. This check keeps it that way. For every event that a pull request
raises, it reads the repository's workflow files and lists each job that

  * belongs to a reviewer workflow (its file, workflow name, job id or job name
    says so),
  * would start on that event for a pull request into a real landing branch
    (the trigger names the event, its branch filter reaches the branch, the job
    ``if`` is not the literal false), and
  * runs on a runner that is not provably GitHub-hosted.

The two cases that matter are named: ``fresh_event_no_lab_job`` reads the live
workflow files, ``planted_trigger`` plants a reviewer trigger into a copy of the
repository's own reviewer workflow and into a minimal one, and requires the
scan to name it.
"""

from __future__ import annotations

import fnmatch
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"

# Events a pull request raises, directly or through a follow-on workflow.
PULL_REQUEST_EVENTS = (
    "pull_request",
    "pull_request_target",
    "pull_request_review",
    "pull_request_review_comment",
    "issue_comment",
    "workflow_run",
    "merge_group",
)
# Events whose branch filter names the pull request's base branch.
BASE_FILTERED_EVENTS = frozenset({"pull_request", "pull_request_target", "merge_group"})
LANDING_BRANCHES = ("dev", "main")
REVIEWER_NAME = re.compile(r"review", re.IGNORECASE)
HOSTED_LABEL = re.compile(r"^(ubuntu|macos|windows)-(latest|[0-9]+(\.[0-9]+)*)(-arm)?$")
REVIEWER_WORKFLOW = "hostile-reviewer.yml"


@dataclass(frozen=True)
class LabJob:
    workflow: str
    job: str
    event: str
    branch: str
    runs_on: str


def _triggers(data: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML reads the bare key ``on`` as the boolean True.
    raw = data.get("on", data.get(True))
    if raw is None:
        return {}
    if isinstance(raw, str):
        return {raw: None}
    if isinstance(raw, list):
        return {str(item): None for item in raw}
    return {str(key): value for key, value in raw.items()}


def _matches_ordered(patterns: list[str], branch: str) -> bool:
    """GitHub's filter order: the last pattern that matches decides, and a ``!``
    pattern excludes what an earlier one included."""
    reached = False
    for pattern in patterns:
        negated = pattern.startswith("!")
        if fnmatch.fnmatchcase(branch, pattern[1:] if negated else pattern):
            reached = not negated
    return reached


def _branch_reached(spec: Any, branch: str) -> bool:
    if not isinstance(spec, dict):
        return True
    names = spec.get("branches")
    ignored = spec.get("branches-ignore")
    if names is not None and not _matches_ordered(list(names), branch):
        return False
    return not (
        ignored is not None and any(fnmatch.fnmatchcase(branch, p) for p in ignored)
    )


def _never_runs(condition: Any) -> bool:
    if condition is False:
        return True
    text = re.sub(r"^\$\{\{|\}\}$", "", str(condition).strip()).strip()
    return text.lower() == "false"


def _runs_on_hosted(runs_on: Any) -> bool:
    if isinstance(runs_on, dict):
        # ``{labels: ...}`` selects like its scalar form; a group or an unresolved
        # expression is never proof of hosted placement.
        if set(runs_on) != {"labels"}:
            return False
        runs_on = runs_on["labels"]
    labels = runs_on if isinstance(runs_on, list) else [runs_on]
    return bool(labels) and all(
        isinstance(label, str) and HOSTED_LABEL.match(label) is not None
        for label in labels
    )


def _is_reviewer(
    stem: str, data: dict[Any, Any], job_id: str, job: dict[Any, Any]
) -> bool:
    names = (stem, str(data.get("name", "")), job_id, str(job.get("name", "")))
    return any(REVIEWER_NAME.search(name) for name in names)


def lab_reviewer_jobs(workflows: Path, event: str, branch: str) -> list[LabJob]:
    """Reviewer jobs that start on ``event`` for a pull request into ``branch``
    and do not run on a provably hosted runner."""
    found: list[LabJob] = []
    for path in sorted(workflows.glob("*.y*ml")):
        data = yaml.safe_load(path.read_text())
        if not isinstance(data, dict):
            continue
        triggers = _triggers(data)
        if event not in triggers:
            continue
        if event in BASE_FILTERED_EVENTS and not _branch_reached(
            triggers[event], branch
        ):
            continue
        for job_id, job in (data.get("jobs") or {}).items():
            if not isinstance(job, dict) or not _is_reviewer(
                path.stem, data, str(job_id), job
            ):
                continue
            if "if" in job and _never_runs(job["if"]):
                continue
            runs_on = job.get("runs-on")
            if _runs_on_hosted(runs_on):
                continue
            found.append(LabJob(path.name, str(job_id), event, branch, str(runs_on)))
    return found


def _reviewer_source() -> dict[Any, Any]:
    """The repository's reviewer workflow when present, otherwise a minimal stand-in.

    Either way the controls plant their own trigger and runner, so deleting the
    production workflow or moving its job to a hosted runner cannot break them."""
    path = WORKFLOWS / REVIEWER_WORKFLOW
    if path.is_file():
        data = yaml.safe_load(path.read_text())
        if (
            isinstance(data, dict)
            and isinstance(data.get("jobs"), dict)
            and data["jobs"]
        ):
            return data
    return {
        "name": "Hostile Reviewer",
        "on": {"workflow_dispatch": None},
        "jobs": {"review": {"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}},
    }


def _plant(source: dict[Any, Any], target: Path) -> None:
    """Write ``source`` to ``target`` with a pull_request trigger on every branch
    and every job on a lab runner."""
    data = dict(source)
    data.pop("on", None)
    data.pop(True, None)
    data["on"] = {"pull_request": {"types": ["opened", "synchronize"]}}
    data["jobs"] = {
        job_id: {**job, "runs-on": ["self-hosted", "omnibase-ci"]}
        for job_id, job in source["jobs"].items()
        if isinstance(job, dict)
    }
    target.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.mark.parametrize("branch", LANDING_BRANCHES)
@pytest.mark.parametrize("event", PULL_REQUEST_EVENTS)
def test_fresh_event_no_lab_job(event: str, branch: str) -> None:
    assert WORKFLOWS.is_dir(), WORKFLOWS
    assert lab_reviewer_jobs(WORKFLOWS, event, branch) == []


def test_fresh_event_no_lab_job_reads_reviewer_workflows() -> None:
    """Positive control: the scan recognises a reviewer workflow by its own markers,
    whether or not this repository still ships ``hostile-reviewer.yml``."""
    assert WORKFLOWS.is_dir(), WORKFLOWS
    data = _reviewer_source()
    assert any(
        _is_reviewer(Path(REVIEWER_WORKFLOW).stem, data, str(job_id), job)
        for job_id, job in data["jobs"].items()
    )


def test_planted_trigger_on_the_repository_reviewer_fails(tmp_path: Path) -> None:
    _plant(_reviewer_source(), tmp_path / REVIEWER_WORKFLOW)
    for branch in LANDING_BRANCHES:
        planted = lab_reviewer_jobs(tmp_path, "pull_request", branch)
        assert planted, f"a planted pull_request trigger on {branch} was not named"
        assert {job.workflow for job in planted} == {REVIEWER_WORKFLOW}


@pytest.mark.parametrize("event", PULL_REQUEST_EVENTS)
def test_planted_trigger_on_every_event_fails(tmp_path: Path, event: str) -> None:
    (tmp_path / "planted-reviewer.yml").write_text(
        yaml.safe_dump(
            {
                "name": "Planted Reviewer",
                "on": {event: None},
                "jobs": {
                    "review": {
                        "runs-on": ["self-hosted", "omnibase-ci"],
                        "steps": [{"run": "true"}],
                    }
                },
            },
            sort_keys=False,
        )
    )
    assert lab_reviewer_jobs(tmp_path, event, "dev")


def test_planted_trigger_controls_are_not_flagged(tmp_path: Path) -> None:
    """Negative controls: a hosted runner, a dead job and a branch no pull request targets."""
    base = {"name": "Planted Reviewer", "on": {"pull_request": None}}
    cases = {
        "hosted.yml": {
            **base,
            "jobs": {"review": {"runs-on": "ubuntu-latest", "steps": []}},
        },
        "dead.yml": {
            **base,
            "jobs": {
                "review": {
                    "if": "${{ false }}",
                    "runs-on": ["self-hosted"],
                    "steps": [],
                }
            },
        },
        "other-branch.yml": {
            "name": "Planted Reviewer",
            "on": {"pull_request": {"branches": ["review-stopped"]}},
            "jobs": {"review": {"runs-on": ["self-hosted"], "steps": []}},
        },
    }
    for name, workflow in cases.items():
        (tmp_path / name).write_text(yaml.safe_dump(workflow, sort_keys=False))
    for branch in LANDING_BRANCHES:
        assert lab_reviewer_jobs(tmp_path, "pull_request", branch) == []


@pytest.mark.parametrize(
    ("branches", "reached_dev", "reached_main"),
    [
        (["**", "!dev", "!main"], False, False),
        (["**", "!dev"], False, True),
        (["!dev", "**"], True, True),
        (["**", "!dev", "dev"], True, True),
        (["main"], False, True),
    ],
)
def test_branch_filter_keeps_ordered_exclusions(
    branches: list[str], reached_dev: bool, reached_main: bool
) -> None:
    """OMN-22946: a later ``!`` pattern excludes and a later plain one re-includes."""
    spec = {"branches": branches}
    assert _branch_reached(spec, "dev") is reached_dev
    assert _branch_reached(spec, "main") is reached_main


@pytest.mark.parametrize(
    ("runs_on", "hosted"),
    [
        ("ubuntu-latest", True),
        ({"labels": "ubuntu-latest"}, True),
        ({"labels": ["ubuntu-latest"]}, True),
        ({"labels": ["self-hosted", "omnibase-ci"]}, False),
        ({"group": "lab-runners"}, False),
        ({"group": "lab-runners", "labels": "ubuntu-latest"}, False),
        ({"labels": "${{ matrix.runner }}"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_runs_on_mapping_classifies_like_its_scalar_form(
    runs_on: Any, hosted: bool
) -> None:
    assert _runs_on_hosted(runs_on) is hosted


def test_hosted_label_mapping_job_is_not_flagged(tmp_path: Path) -> None:
    (tmp_path / "mapped.yml").write_text(
        yaml.safe_dump(
            {
                "name": "Planted Reviewer",
                "on": {"pull_request": None},
                "jobs": {
                    "review": {"runs-on": {"labels": "ubuntu-latest"}, "steps": []}
                },
            },
            sort_keys=False,
        )
    )
    for branch in LANDING_BRANCHES:
        assert lab_reviewer_jobs(tmp_path, "pull_request", branch) == []


def test_controls_do_not_need_the_production_reviewer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OMN-22946: deleting the production reviewer, or moving its job to a
    hosted runner, leaves the detection controls passing."""
    for workflows in (tmp_path / "absent", tmp_path / "hosted"):
        workflows.mkdir()
    (tmp_path / "hosted" / REVIEWER_WORKFLOW).write_text(
        yaml.safe_dump(
            {
                "name": "Hostile Reviewer",
                "on": {"workflow_dispatch": None},
                "jobs": {"review": {"runs-on": "ubuntu-latest", "steps": []}},
            },
            sort_keys=False,
        )
    )
    for name in ("absent", "hosted"):
        monkeypatch.setattr(sys.modules[__name__], "WORKFLOWS", tmp_path / name)
        test_fresh_event_no_lab_job_reads_reviewer_workflows()
        planted = tmp_path / f"planted-{name}"
        planted.mkdir()
        test_planted_trigger_on_the_repository_reviewer_fails(planted)
