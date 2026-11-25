# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the lint steps of ``.github/workflows/ci.yml`` wherever they run (OMN-24562).

The required ``lint`` context is an aggregate job that waits for parallel group jobs named
``lint-group-<n>``. A test that asks "is this gate step wired into lint" reads every step of every
group job through :func:`lint_steps`, so splitting or moving steps between groups does not change
what it asserts. The aggregate's own steps are included.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

LINT_AGGREGATE = "lint"
LINT_GROUP_PREFIX = "lint-group-"


def lint_group_ids(jobs: Mapping[str, Any]) -> list[str]:
    """The ids of the parallel lint group jobs, in numeric order."""
    ids = [name for name in jobs if name.startswith(LINT_GROUP_PREFIX)]
    return sorted(ids, key=lambda name: int(name[len(LINT_GROUP_PREFIX) :]))


def lint_jobs(workflow: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The aggregate ``lint`` job followed by every group job."""
    jobs = workflow["jobs"]
    return [jobs[LINT_AGGREGATE], *(jobs[name] for name in lint_group_ids(jobs))]


def lint_steps(workflow: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every step of the lint aggregate and its group jobs."""
    return [step for job in lint_jobs(workflow) for step in job.get("steps", [])]


def lint_job_with_step(
    workflow: Mapping[str, Any], step_name: str
) -> dict[str, Any] | None:
    """The lint job (group or aggregate) that carries the step named ``step_name``."""
    for job in lint_jobs(workflow):
        if any(step.get("name") == step_name for step in job.get("steps", [])):
            return job
    return None
