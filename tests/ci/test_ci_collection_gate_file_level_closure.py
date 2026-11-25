# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The whole-tree collection gate is one ci.yml job that follows the detect job, never a matrix cell.

With the root conftest edge cut, a closure that reaches ``tests/conftest.py`` selects no test of its
own beyond the closure, so a whole-tree ``pytest --collect-only`` catches an import break anywhere.
It runs once per pull request: a collection step inside the sharded test job would run once per
shard.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
GATE_JOB = "collection-gate"
DETECT_JOB = "detect-changes"


def _jobs() -> dict[str, dict[str, Any]]:
    jobs: dict[str, dict[str, Any]] = yaml.safe_load(CI.read_text(encoding="utf-8"))[
        "jobs"
    ]
    return jobs


def _collects(job: dict[str, Any]) -> list[str]:
    return [
        step["run"]
        for step in job.get("steps", [])
        if "pytest" in step.get("run", "") and "--collect-only" in step.get("run", "")
    ]


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def test_file_level_closure_collection_gate_is_one_job_after_detect() -> None:
    jobs = _jobs()
    gate = jobs[GATE_JOB]
    assert DETECT_JOB in _needs(gate)
    assert len(_collects(gate)) == 1
    assert "tests/" in _collects(gate)[0]
    assert "matrix" not in gate.get("strategy", {})
    # the one job that collects after detect, and no other
    after_detect = [
        name
        for name, job in jobs.items()
        if _collects(job) and DETECT_JOB in _needs(job)
    ]
    assert after_detect == [GATE_JOB]


def test_file_level_closure_no_matrix_job_collects() -> None:
    sharded = {
        name: job
        for name, job in _jobs().items()
        if "matrix" in job.get("strategy", {})
    }
    assert "test" in sharded, "the sharded test job is the matrix this guards"
    offenders = [name for name, job in sharded.items() if _collects(job)]
    assert offenders == [], (
        f"a collection step inside a matrix job runs per shard: {offenders}"
    )


def test_file_level_closure_gate_runs_once_per_pull_request_not_per_shard() -> None:
    gate = _jobs()[GATE_JOB]
    condition = gate["if"]
    assert "needs.detect-changes.outputs.collection_gate == 'true'" in condition
    assert "needs.detect-changes.result == 'success'" in condition
    detect_outputs = _jobs()[DETECT_JOB]["outputs"]
    assert "collection_gate" in detect_outputs
