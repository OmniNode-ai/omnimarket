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

import re
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]

# workflow file -> job id -> the exact labels that job must pin.
#
# Add a row here when a job's correctness depends on WHERE its runner sits.
# Never remove one to make a routing flip land: a lane-bound job that has to
# move needs its labels changed here, in the same commit, deliberately.
# arch-amd64 selects the LAN amd64 pools: the h203-light runners that joined
# omnibase-ci on 2026-10-24 are off-LAN and lack this label.
LANE_BOUND_JOBS: dict[str, dict[str, list[str]]] = {
    ".github/workflows/delegation-regression-nightly.yml": {
        "golden-tasks": ["self-hosted", "omnibase-ci", "arch-amd64"],
    },
    # OMN-19440: the daily command-to-terminal join reads the lab lane broker.
    ".github/workflows/delegation-terminal-join-daily.yml": {
        "terminal-join": ["self-hosted", "omnibase-ci", "arch-amd64"],
    },
    # The rungs are lab hosts.
    ".github/workflows/delegation-rung-eval-nightly.yml": {
        "rung-eval": ["self-hosted", "omnibase-ci", "arch-amd64"],
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


# OMN-22478: a floating hosted image in a job that installs a versioned system
# package. On 2026-11-06 about 22:11Z GitHub's ``ubuntu-latest`` became Ubuntu
# 26.04, which has no ``postgresql-16`` package, and every full-suite shard that
# ran ``apt-get install postgresql-16`` failed. The jobs were built for 24.04.
_FLOATING_IMAGES = frozenset(
    {
        "ubuntu-latest",
        "macos-latest",
        "windows-latest",
        "ubuntu",
        "macos",
        "windows",
    }
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9._-]*")
_MATRIX_REF = re.compile(r"matrix\.([A-Za-z_][A-Za-z0-9_-]*)")
_COMMAND_SPLIT = re.compile(r"&&|\|\||;|\|")
_PKG_EQUALS_VERSION = re.compile(r"^[A-Za-z0-9][\w.+-]*=\S+$")
_PKG_AT_VERSION = re.compile(r"^[\w.+-]+@\d")
_PKG_HYPHEN_VERSION = re.compile(r"[A-Za-z]-\d+(\.\d+)*$")
_PKG_DOTTED_VERSION = re.compile(r"[A-Za-z]\d*\.\d+")
_REPOSITORY_COMMANDS = ("add-apt-repository", "apt-add-repository")


def _is_versioned_package(token: str) -> bool:
    """True for a package argument named with its version; options and paths are not."""
    if not token or token.startswith("-") or "$" in token or "{" in token:
        return False
    if "=" in token:
        return "::" not in token and bool(_PKG_EQUALS_VERSION.match(token))
    if "/" in token or ":" in token:
        return False
    return bool(
        _PKG_AT_VERSION.match(token)
        or _PKG_HYPHEN_VERSION.search(token)
        or _PKG_DOTTED_VERSION.search(token)
    )


def _versioned_packages(run: str) -> list[str]:
    """Return the versioned system packages one ``run`` script installs.

    A versioned package is named with its version: ``postgresql-16``,
    ``python3.12``, ``libpq-dev=16.1``, ``openssl@3``. Adding an apt repository
    counts too: it exists to reach a package the base image lacks.
    """
    found: list[str] = []
    for command in _COMMAND_SPLIT.split(run.replace("\\\n", " ")):
        tokens = command.split()
        if any(token in _REPOSITORY_COMMANDS for token in tokens):
            found.append(next(t for t in tokens if t in _REPOSITORY_COMMANDS))
            continue
        for index, token in enumerate(tokens[:-1]):
            manager = token in ("apt", "apt-get") or (
                token == "brew" and tokens[index + 1] == "install"
            )
            if not manager or "install" not in tokens[index:]:
                continue
            installed = tokens[index:].index("install") + index + 1
            found.extend(
                package
                for package in (raw.strip("\"'") for raw in tokens[installed:])
                if _is_versioned_package(package)
            )
            break
    return found


def _matrix_values(job: dict[str, Any], key: str) -> list[str]:
    strategy = job.get("strategy")
    matrix = strategy.get("matrix") if isinstance(strategy, dict) else None
    if not isinstance(matrix, dict):
        return []
    values = matrix.get(key)
    found = [str(v) for v in values] if isinstance(values, list) else []
    include = matrix.get("include")
    if isinstance(include, list):
        found.extend(
            str(entry[key])
            for entry in include
            if isinstance(entry, dict) and key in entry
        )
    return found


def _floating_labels(job: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Return ``(label, matrix key or None)`` for every floating hosted label."""
    runs_on = job.get("runs-on")
    if isinstance(runs_on, dict):
        runs_on = runs_on.get("labels")
    items = runs_on if isinstance(runs_on, list) else [runs_on]
    found: list[tuple[str, str | None]] = []
    for item in items:
        if not isinstance(item, str):
            continue
        for word in _WORD.findall(item):
            if word in _FLOATING_IMAGES:
                found.append((word, None))
        for match in _MATRIX_REF.finditer(item):
            key = match.group(1)
            found.extend(
                (value.strip(), key)
                for value in _matrix_values(job, key)
                if value.strip() in _FLOATING_IMAGES
            )
    return found


def _leg_steps(
    job: dict[str, Any], label: str, key: str | None
) -> list[dict[str, Any]]:
    """Steps that run on the leg: a step guarded to another matrix value does not."""
    steps = [s for s in job.get("steps") or [] if isinstance(s, dict)]
    if key is None:
        return steps
    on_other_leg = re.compile(rf"matrix\.{re.escape(key)}\b")
    on_this_leg = re.compile(
        rf"matrix\.{re.escape(key)}\s*==\s*['\"]{re.escape(label)}['\"]"
    )
    return [
        s
        for s in steps
        if not on_other_leg.search(str(s.get("if", "")))
        or on_this_leg.search(str(s.get("if", "")))
    ]


def check_workflow_image_pins(rel: str, document: Any) -> list[str]:
    """Return the floating-image violations of one parsed workflow."""
    jobs = document.get("jobs") if isinstance(document, dict) else None
    if not isinstance(jobs, dict):
        return []
    violations: list[str] = []
    for job_id, job in jobs.items():
        if not isinstance(job, dict):
            continue
        reported: set[str] = set()
        for label, key in _floating_labels(job):
            if label in reported:
                continue
            packages: list[str] = []
            for step in _leg_steps(job, label, key):
                run = step.get("run")
                if isinstance(run, str):
                    packages.extend(_versioned_packages(run))
            if packages:
                reported.add(label)
                violations.append(
                    f"{rel}: job {job_id!r}: runs-on {label!r} is a floating hosted "
                    f"image but the job installs {sorted(set(packages))}; pin the "
                    "image the job was built for (for example ubuntu-24.04) "
                    "(OMN-22478)"
                )
    return violations


def check_hosted_image_pins(root: Path | None = None) -> list[str]:
    """Return every floating-image violation across all workflow files."""
    base = root if root is not None else REPO_ROOT
    violations: list[str] = []
    workflows = base / ".github" / "workflows"
    for path in sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")]):
        rel = path.relative_to(base).as_posix()
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            violations.append(f"{rel}: workflow does not parse: {error}")
            continue
        violations.extend(check_workflow_image_pins(rel, document))
    return violations


def main() -> int:
    violations = check_repo()
    if violations:
        print("Lane-bound CI jobs must pin literal runner labels (OMN-18349):")
        for violation in violations:
            print(f"  - {violation}")
    pin_violations = check_hosted_image_pins()
    if pin_violations:
        print(
            "A job that installs a versioned system package must not float its "
            "hosted image (OMN-22478):"
        )
        for violation in pin_violations:
            print(f"  - {violation}")
    if violations or pin_violations:
        return 1
    checked = sum(len(jobs) for jobs in LANE_BOUND_JOBS.values())
    print(f"lane-bound job placement OK ({checked} job(s) checked)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
