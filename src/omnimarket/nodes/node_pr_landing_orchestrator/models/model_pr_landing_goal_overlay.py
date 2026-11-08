# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing goal and the repository scope of the landing nodes: every setting an overlay value, none a default.

Rebuild piece 9 (landing and merge queue), plan section 3.6 row 9: the overlay keys ``landing_goal`` and ``repo_scope``.
``landing_goal`` holds the hourly requirements a landing reading is judged against; ``repo_scope`` holds which
repositories, and which kind of repository, the landing nodes act on. An absent key refuses the load and names it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelLandingGoal(BaseModel):
    """What landing must achieve, by the hour."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The goal: this many in-scope merges inside each window.
    merges_per_window: int = Field(ge=1)
    #: The window the goal and the head bound are counted over, in seconds.
    window_seconds: int = Field(ge=1)
    #: From this many waiting eligible pull requests up, the merge rate is judged; below it the daily canary is.
    waiting_threshold: int = Field(ge=1)
    #: A head green on every required context that stays unmerged longer than this reads NOT WORKING.
    eligible_head_bound_seconds: int = Field(ge=1)
    #: The synthetic canary pull request must land inside this long.
    canary_bound_seconds: int = Field(ge=1)


class ModelLandingRepoScope(BaseModel):
    """Which repositories the landing nodes act on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: ``owner/repo`` names in scope; a repository not listed is out of scope.
    repositories: tuple[str, ...] = Field(min_length=1)
    #: ``all`` keeps both kinds; ``public`` or ``private`` keeps only that kind of listed repository.
    visibility: Literal["all", "public", "private"]

    def in_scope(self, repository: str, *, public: bool) -> bool:
        """True when ``repository`` is listed and of the visibility the scope keeps."""
        if repository not in self.repositories:
            return False
        if self.visibility == "all":
            return True
        return public if self.visibility == "public" else not public


class ModelPrLandingGoalOverlay(BaseModel):
    """The overlay document of node_pr_landing_orchestrator: the goal and the scope, both required."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    landing_goal: ModelLandingGoal
    repo_scope: ModelLandingRepoScope


__all__ = [
    "ModelLandingGoal",
    "ModelLandingRepoScope",
    "ModelPrLandingGoalOverlay",
]
