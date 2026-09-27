# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Check-run facts read by the read_head_checks operation (OMN-19826).

Facts only. The per-check verdict belongs to the triage compute's
classify_head_checks operation, not to this effect.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
            facts.append(
                cls(
                    check_run_id=run["id"],
                    name=run["name"],
                    status=run["status"],
                    conclusion=run.get("conclusion"),
                    head_sha=run["head_sha"],
                    details_url=run.get("details_url"),
                    app_slug=app.get("slug") if isinstance(app, dict) else None,
                    check_suite_id=suite.get("id") if isinstance(suite, dict) else None,
                )
            )
        return tuple(facts)
