#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Refuse a lane-probing CI job whose runner placement is variable-driven (OMN-18349).

A job that opens a socket to the lab network is only correct on a runner that
sits inside that network. That is a NETWORK-NAMESPACE fact about the runner, not
a trust fact about the code, so it must not be expressed through the shared
single-owner runner-routing variables (omni_home CLAUDE.md rule 14): those are
owned by whichever lane last flipped them, and a flip moves every job that reads
them at once, silently.

That is not hypothetical. ``delegation-regression-nightly.yml`` read
``vars.OMNI_TRUSTED_CI_RUNS_ON_JSON``; when that variable was set to
``["ubuntu-latest"]`` at org scope on 2026-08-27 the job moved onto a
GitHub-hosted runner with no route to the lab, and every subsequent run died on
a Postgres connect timeout that read like a delegation regression. Nobody
noticed for 18 nights because the workflow had never been green.

This checker is the mechanical half of that fix: the jobs listed in
``LANE_BOUND_JOBS`` must pin their labels as literals. It runs as the
``lane-job-placement`` pre-commit hook and, through
``tests/test_delegation_nightly_placement.py``, inside the repo's required
pytest job -- the same ``check_repo`` predicate on both sides, so a local pass
and a CI pass cannot disagree. Re-introducing the variable read is a red build
rather than a review catch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# workflow file -> job id -> the exact labels that job must pin.
#
# Add a row here when a job's correctness depends on WHERE its runner sits.
# Never remove one to make a routing flip land: a lane-bound job that has to
# move needs its labels changed here, in the same commit, deliberately.
LANE_BOUND_JOBS: dict[str, dict[str, list[str]]] = {
    ".github/workflows/delegation-regression-nightly.yml": {
        "golden-tasks": ["self-hosted", "omnibase-ci"],
    },
}

# A runs-on value carrying an expression is variable-driven whatever it
# evaluates to today, which is exactly the property this checker refuses.
_EXPRESSION_MARKER = "${{"


def check_runs_on(runs_on: Any, expected: list[str]) -> list[str]:
    """Return the violations for one job's ``runs-on`` value (empty == clean)."""
    if isinstance(runs_on, str):
        if _EXPRESSION_MARKER in runs_on:
            return [
                "runs-on is a GitHub Actions expression, so this job's runner "
                "placement is owned by a routing variable rather than by this "
                f"file. Pin the literal labels {expected} instead."
            ]
        actual = [runs_on]
    elif isinstance(runs_on, list):
        if any(_EXPRESSION_MARKER in str(item) for item in runs_on):
            return [
                "runs-on contains a GitHub Actions expression; pin the literal "
                f"labels {expected} instead."
            ]
        actual = [str(item) for item in runs_on]
    else:
        return [f"runs-on has unsupported type {type(runs_on).__name__}"]

    if actual != expected:
        return [f"runs-on is {actual}, expected the literal {expected}"]
    return []


def check_repo(root: Path | None = None) -> list[str]:
    """Return every placement violation across the declared lane-bound jobs."""
    base = root if root is not None else REPO_ROOT
    violations: list[str] = []
    for workflow_rel, jobs in LANE_BOUND_JOBS.items():
        path = base / workflow_rel
        if not path.exists():
            violations.append(f"{workflow_rel}: declared lane-bound but missing")
            continue
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        declared_jobs = document.get("jobs") if isinstance(document, dict) else None
        if not isinstance(declared_jobs, dict):
            violations.append(f"{workflow_rel}: no jobs block")
            continue
        for job_id, expected in jobs.items():
            job = declared_jobs.get(job_id)
            if not isinstance(job, dict):
                violations.append(f"{workflow_rel}: job {job_id!r} is missing")
                continue
            if "runs-on" not in job:
                violations.append(f"{workflow_rel}: job {job_id!r} declares no runs-on")
                continue
            for problem in check_runs_on(job["runs-on"], expected):
                violations.append(f"{workflow_rel}: job {job_id!r}: {problem}")
    return violations


def main() -> int:
    violations = check_repo()
    if violations:
        print("Lane-bound CI jobs must pin literal runner labels (OMN-18349):")
        for violation in violations:
            print(f"  - {violation}")
        return 1
    checked = sum(len(jobs) for jobs in LANE_BOUND_JOBS.values())
    print(f"lane-bound job placement OK ({checked} job(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
