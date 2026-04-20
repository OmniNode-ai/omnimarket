# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Claimed PR model for agent self-reports verified by overseer.

Agents report `(pr_number, repo)` pairs they believe are green. The
overseer verifier shells out to `gh pr checks` to prove or disprove
each claim before issuing a PASS verdict.

Related:
    - OMN-9273: Wire gh pr checks against agent self-reports
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelClaimedPr(BaseModel):
    """A PR an agent reports as green; subject to live `gh pr checks` verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int = Field(..., ge=1, description="Pull request number.")
    repo: str = Field(
        ...,
        description="Repo in `owner/name` form passed verbatim to `gh --repo`.",
    )


__all__: list[str] = ["ModelClaimedPr"]
