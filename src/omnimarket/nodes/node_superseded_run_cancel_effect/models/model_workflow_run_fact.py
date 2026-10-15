# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One queued or in-progress workflow run, as the run list gives it (OMN-17427)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkflowRunFact(BaseModel):
    """The fields of a GitHub workflow run the supersede rule reads, and no others."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: int
    workflow_name: str = ""
    event: str
    status: str
    head_sha: str = Field(min_length=1)
    head_branch: str = ""
    created_at: str = Field(description="ISO-8601 UTC; orders the runs of one PR.")
    pr_numbers: tuple[int, ...] = ()

    @classmethod
    def from_github(cls, body: dict[str, object]) -> ModelWorkflowRunFact | None:
        """Parse one entry of a workflow-runs list; None when it lacks a field the rule needs."""
        run_id = body.get("id")
        event = body.get("event")
        head_sha = body.get("head_sha")
        created_at = body.get("created_at")
        if not (
            isinstance(run_id, int)
            and isinstance(event, str)
            and isinstance(head_sha, str)
            and head_sha
            and isinstance(created_at, str)
            and created_at
        ):
            return None
        prs = body.get("pull_requests")
        numbers: list[int] = []
        if isinstance(prs, list):
            for pr in prs:
                number = pr.get("number") if isinstance(pr, dict) else None
                if isinstance(number, int):
                    numbers.append(number)
        name = body.get("name")
        branch = body.get("head_branch")
        status = body.get("status")
        return cls(
            run_id=run_id,
            workflow_name=name if isinstance(name, str) else "",
            event=event,
            status=status if isinstance(status, str) else "",
            head_sha=head_sha,
            head_branch=branch if isinstance(branch, str) else "",
            created_at=created_at,
            pr_numbers=tuple(sorted(set(numbers))),
        )
