# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CI wiring for the native PostgreSQL 16 shadow-migration proof."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
_JOB_KEYS = ("test", "integration-guard")
_PROVISION_STEP = "Provision PostgreSQL 16 server tools for native migration proofs"
_TEST_STEP_BY_JOB = {
    "test": "Run pytest (full suite)",
    "integration-guard": "Run integration-marked tests (Postgres provisioned)",
}


def _steps(job_key: str, workflow: Path = CI_WORKFLOW) -> list[dict[str, Any]]:
    document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    jobs = document["jobs"]
    assert isinstance(jobs, dict)
    job = jobs[job_key]
    assert isinstance(job, dict)
    steps = job["steps"]
    assert isinstance(steps, list)
    return steps


@pytest.mark.parametrize("job_key", _JOB_KEYS)
def test_pg16_server_tools_precede_native_proofs(job_key: str) -> None:
    """The PG service image alone cannot supply initdb/pg_ctl to the runner."""
    steps = _steps(job_key)
    provision = [step for step in steps if step.get("name") == _PROVISION_STEP]
    assert len(provision) == 1, f"{job_key} needs an explicit server-tools step"
    run = str(provision[0].get("run", ""))

    assert "sudo apt-get install --yes postgresql-16" in run
    assert "pg16_bin=/usr/lib/postgresql/16/bin" in run
    assert "for tool in initdb pg_ctl psql; do" in run
    assert 'test -x "${pg16_bin}/${tool}"' in run
    assert "PostgreSQL\\) 16\\." in run
    assert 'echo "${pg16_bin}" >> "$GITHUB_PATH"' in run

    test_steps = [
        step for step in steps if step.get("name") == _TEST_STEP_BY_JOB[job_key]
    ]
    assert len(test_steps) == 1
    assert steps.index(provision[0]) < steps.index(test_steps[0])
