# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR coverage census comes from shard artifacts; the full run is nightly (OMN-18556).

Three couplings keep the cut-over honest, and each is a rename or a one-line
edit away from breaking silently, so each is pinned here:

1. **One census on the PR path.** ``Coverage Sweep Gate`` in ``ci.yml`` runs
   ``aggregate_coverage_artifacts.py`` over the ``test`` shards' artifacts and
   nothing on the PR path runs ``run_coverage_sweep_gate.py`` (the second full
   ``pytest --cov``). The advisory shadow job is gone.
2. **Still required.** The name stays in ``STRICT_GATE_JOBS`` and in the
   docs-only tier, never in ``SKIPPABLE_GATE_JOBS``, so a skipped or missing
   census fails ``CI Summary`` closed exactly as before. ``SOFT_ALLOWLIST`` is
   empty, so no job fails without failing the summary.
3. **The full suite still runs.** ``nightly-full-suite.yml`` runs the full
   selection on ``dev`` on a schedule, with no opt-in input, and can never
   become a PR context.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
NIGHTLY_WORKFLOW = REPO_ROOT / ".github/workflows/nightly-full-suite.yml"

sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import ci_summary_gate  # noqa: E402

GATE_NAME = "Coverage Sweep Gate"
FULL_RUN_SCRIPT = "scripts/ci/run_coverage_sweep_gate.py"
AGGREGATE_SCRIPT = "scripts/ci/aggregate_coverage_artifacts.py"


def _load(path: Path) -> dict[Any, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{path.name} did not parse to a mapping"
    return data


def _triggers(workflow: dict[Any, Any]) -> dict[str, Any]:
    # PyYAML (YAML 1.1) reads a bare ``on:`` key as boolean True.
    raw = workflow.get("on", workflow.get(True))
    assert isinstance(raw, dict), "workflow triggers must be a mapping"
    return raw


def _run_text(job: dict[str, Any]) -> str:
    return "\n".join(str(step.get("run", "")) for step in job.get("steps", []))


def _jobs_named(
    workflow: dict[Any, Any], name: str
) -> list[tuple[str, dict[str, Any]]]:
    return [
        (job_id, job)
        for job_id, job in workflow["jobs"].items()
        if job.get("name") == name
    ]


def test_pr_census_aggregates_shard_artifacts() -> None:
    ci = _load(CI_WORKFLOW)
    matches = _jobs_named(ci, GATE_NAME)
    assert len(matches) == 1, f"expected exactly one {GATE_NAME!r} job, got {matches}"
    _, job = matches[0]

    assert "test" in job["needs"], "the census must wait for the test shards"
    assert "detect-changes" in job["needs"], "split_count comes from detect-changes"
    run = _run_text(job)
    assert AGGREGATE_SCRIPT in run
    assert "--expected-head" in run
    assert "--split-count" in run
    assert FULL_RUN_SCRIPT not in run
    # The authoritative artifact no longer exists, so there is nothing to compare to.
    assert "--compare-to" not in run


def _shard_guard_step() -> dict[str, Any]:
    (_, job) = _jobs_named(_load(CI_WORKFLOW), GATE_NAME)[0]
    steps = job["steps"]
    guards = [
        s for s in steps if s.get("name") == "Refuse when the test shards did not run"
    ]
    assert len(guards) == 1
    assert steps.index(guards[0]) == 0, "the refusal must run before any clone or sync"
    return guards[0]


@pytest.mark.parametrize(
    ("detect", "split_count", "expected_rc"),
    [
        ("skipped", "", 1),  # OCC preflight failed: no shard ever ran
        ("success", "", 1),  # selector succeeded but published no split count
        ("failure", "20", 1),
        ("success", "20", 0),
    ],
)
def test_census_refuses_by_name_when_shards_did_not_run(
    detect: str, split_count: str, expected_rc: int
) -> None:
    step = _shard_guard_step()
    assert set(step["env"]) == {"DETECT_RESULT", "TEST_RESULT", "SPLIT_COUNT"}
    env = {
        "PATH": "/usr/bin:/bin",
        "DETECT_RESULT": detect,
        "TEST_RESULT": "skipped",
        "SPLIT_COUNT": split_count,
    }
    proc = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == expected_rc, proc.stdout + proc.stderr
    if expected_rc:
        assert "coverage census refused" in proc.stdout


def test_no_pr_job_runs_the_full_coverage_suite() -> None:
    ci = _load(CI_WORKFLOW)
    offenders = [
        job_id
        for job_id, job in ci["jobs"].items()
        if FULL_RUN_SCRIPT in _run_text(job)
    ]
    assert offenders == [], (
        f"ci.yml jobs still run a second full pytest --cov: {offenders}"
    )
    assert _jobs_named(ci, "Coverage Aggregate (shadow)") == []


def test_census_stays_a_strict_gate() -> None:
    assert GATE_NAME in ci_summary_gate.STRICT_GATE_JOBS
    assert GATE_NAME not in ci_summary_gate.SKIPPABLE_GATE_JOBS
    assert GATE_NAME in ci_summary_gate.DOCS_ONLY_SKIPPABLE_GATE_JOBS
    assert frozenset() == ci_summary_gate.SOFT_ALLOWLIST


def test_nightly_runs_full_selection_on_dev_only() -> None:
    nightly = _load(NIGHTLY_WORKFLOW)
    triggers = _triggers(nightly)

    assert set(triggers) == {"schedule", "workflow_dispatch"}, triggers
    assert triggers["schedule"], "a nightly needs a cron entry"
    # No opt-in: a dispatch input would let a run narrow the selection.
    assert not triggers["workflow_dispatch"], "workflow_dispatch must take no inputs"

    jobs = nightly["jobs"]
    assert len(jobs) == 1
    (job,) = jobs.values()
    checkout = [
        s
        for s in job["steps"]
        if str(s.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkout) == 1
    assert checkout[0].get("with", {}).get("ref") == "dev"

    run = _run_text(job)
    assert FULL_RUN_SCRIPT in run
    assert "--skip-generate" not in run
    assert "-m " not in run, (
        "marker selection is fixed inside the runner, not passed in"
    )


def test_nightly_is_never_a_pr_context() -> None:
    nightly = _load(NIGHTLY_WORKFLOW)
    triggers = _triggers(nightly)
    for pr_event in ("pull_request", "pull_request_target", "merge_group", "push"):
        assert pr_event not in triggers
    (job,) = nightly["jobs"].values()
    name = job["name"]
    assert name not in ci_summary_gate.STRICT_GATE_JOBS
    assert name not in ci_summary_gate.SKIPPABLE_GATE_JOBS
    assert name not in ci_summary_gate.EXPECTED_EXTERNAL_CONTEXTS
