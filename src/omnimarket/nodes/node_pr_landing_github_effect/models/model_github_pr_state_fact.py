# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The PR snapshot read by the read_pr_state operation (OMN-19829).

Revision 1 of plan 5.1, section 6: the autobind command is a prompt, not a
snapshot, so the landing orchestrator answers every prompt (and every
reconciliation tick) with one conditional read of the pull request. This model
is that read's facts: the head, the base, draft, the title and labels a hold
marker lives in, open or closed, merged, whether auto-merge is armed, GitHub's
mergeable state and the GraphQL node id the arm and disarm mutations take.

Facts only. Whether a title or label is a hold is decided by the orchestrator
(``merge_control.hold_marker``), not by this effect.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class ModelGithubPrStateFact(BaseModel):
    """One pull request as GET /repos/{owner}/{repo}/pulls/{n} reported it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(gt=0)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    base_ref: str = Field(min_length=1)
    state: Literal["open", "closed"]
    merged: bool
    draft: bool
    title: str
    labels: tuple[str, ...] = ()
    auto_merge_enabled: bool = Field(
        description="True when GitHub reports an auto_merge request on the PR."
    )
    mergeable_state: str | None = Field(
        default=None,
        description="GitHub mergeable_state, verbatim (clean, behind, ...).",
    )
    node_id: str = Field(min_length=1, description="GraphQL node id of the PR.")

    @classmethod
    def from_pull_body(cls, body: dict[str, object] | None) -> ModelGithubPrStateFact:
        """Parse a pulls/{n} response body. Raises on a body missing a read field."""
        if body is None:
            raise ValueError("pulls response has no body")
        head = body.get("head")
        base = body.get("base")
        if not isinstance(head, dict) or not isinstance(base, dict):
            raise ValueError("pulls response has no head or base object")
        head_sha = head.get("sha")
        if not isinstance(head_sha, str) or not _FULL_SHA.match(head_sha):
            raise ValueError("pulls response head.sha is not a full sha")
        raw_labels = body.get("labels")
        labels: list[str] = []
        if isinstance(raw_labels, list):
            for label in raw_labels:
                if isinstance(label, dict) and isinstance(label.get("name"), str):
                    labels.append(str(label["name"]))
        return cls.model_validate(
            {
                "pr_number": body.get("number"),
                "head_sha": head_sha,
                "base_ref": base.get("ref"),
                "state": body.get("state"),
                "merged": bool(body.get("merged")),
                "draft": bool(body.get("draft")),
                "title": body.get("title") or "",
                "labels": tuple(labels),
                "auto_merge_enabled": body.get("auto_merge") is not None,
                "mergeable_state": body.get("mergeable_state"),
                "node_id": body.get("node_id"),
            }
        )


__all__: list[str] = ["ModelGithubPrStateFact"]
