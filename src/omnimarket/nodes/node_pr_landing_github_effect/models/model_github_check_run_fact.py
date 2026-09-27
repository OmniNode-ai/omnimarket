# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check-run facts read by the read_head_checks operation (OMN-19826).

Facts only. The per-check verdict belongs to the triage compute's
classify_head_checks operation, not to this effect.

``run_id`` and ``run_attempt`` (contract 1.1.0, OMN-19831, plan revision 1
change F7): the Actions run a check belongs to, parsed from its details URL,
and the run attempt that produced it. The check-runs API carries no attempt, so
the effect reads it from the run's jobs list (every attempt, keyed by job id,
which is the check-run id) for each run with a failed copy. A copy the effect
did not look up keeps ``run_attempt`` None.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

_RUN_IN_DETAILS_URL = re.compile(r"/actions/runs/(\d+)(?:/|$)")

# Conclusions that are not a pass; a run with such a copy gets its attempts read.
FAILED_CONCLUSIONS = frozenset(
    {"failure", "timed_out", "cancelled", "action_required", "startup_failure", "stale"}
)


def run_id_from_details_url(details_url: str | None) -> int | None:
    """The Actions run id in a check run's details URL, or None."""
    if not details_url:
        return None
    match = _RUN_IN_DETAILS_URL.search(details_url)
    return int(match.group(1)) if match else None


class ModelGithubCheckRunFact(BaseModel):
    """One check run on the PR head, as GitHub reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    check_run_id: int = Field(gt=0)
    name: str = Field(min_length=1)
    status: str = Field(min_length=1)
    conclusion: str | None
    head_sha: str = Field(min_length=40, max_length=40)
    details_url: str | None
    app_slug: str | None
    check_suite_id: int | None
    run_id: int | None = Field(default=None, gt=0)
    run_attempt: int | None = Field(default=None, ge=1)

    @property
    def failed(self) -> bool:
        return self.conclusion is not None and self.conclusion in FAILED_CONCLUSIONS

    @classmethod
    def from_check_runs_body(
        cls, body: dict[str, object] | None
    ) -> tuple[ModelGithubCheckRunFact, ...]:
        """Parse the ``check_runs`` array of a check-runs list response."""
        if body is None:
            raise ValueError("check-runs response has no body")
        runs = body.get("check_runs")
        if not isinstance(runs, list):
            raise ValueError("check-runs response has no check_runs array")
        facts: list[ModelGithubCheckRunFact] = []
        for run in runs:
            if not isinstance(run, dict):
                raise ValueError("check_runs entry is not an object")
            app = run.get("app")
            suite = run.get("check_suite")
            details_url = run.get("details_url")
            facts.append(
                cls(
                    check_run_id=run["id"],
                    name=run["name"],
                    status=run["status"],
                    conclusion=run.get("conclusion"),
                    head_sha=run["head_sha"],
                    details_url=details_url,
                    app_slug=app.get("slug") if isinstance(app, dict) else None,
                    check_suite_id=suite.get("id") if isinstance(suite, dict) else None,
                    run_id=run_id_from_details_url(
                        details_url if isinstance(details_url, str) else None
                    ),
                )
            )
        return tuple(facts)


def job_attempts_from_jobs_body(body: dict[str, object] | None) -> dict[int, int]:
    """Map job id (the check-run id) to run_attempt from a run's jobs list."""
    if body is None:
        raise ValueError("jobs response has no body")
    jobs = body.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("jobs response has no jobs array")
    attempts: dict[int, int] = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise ValueError("jobs entry is not an object")
        job_id = job.get("id")
        attempt = job.get("run_attempt")
        if not isinstance(job_id, int) or not isinstance(attempt, int):
            raise ValueError("jobs entry lacks an integer id or run_attempt")
        attempts[job_id] = attempt
    return attempts
