# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing goal's scope, as the goal_scope overlay declares it (OMN-23734).

Shared by the hourly check's facts step (which reads the overlay) and its goal check (which judges pull
requests against it). The lists are the overlay's; nothing here holds a value.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

IN_SCOPE = "in-scope"
PAUSED = "paused"
UNLISTED = "unlisted"


class ModelGoalScope(BaseModel):
    """``repos`` hold every pull request in scope; ``prs`` and ``prerequisites`` name pull requests (``repo#n``);
    ``paused`` are the ones the draft pause lane drafted; the kinds are the lab-fill kinds the goal admits."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ruling: str
    repos: frozenset[str] = frozenset()
    prs: frozenset[str] = frozenset()
    prerequisites: frozenset[str] = frozenset()
    paused: frozenset[str] = frozenset()
    repo_work_kinds: frozenset[str] = frozenset()
    prerequisite_kinds: frozenset[str] = frozenset()

    def repo_whole(self, repo: str) -> bool:
        """Every pull request of the repository is in scope: it is listed, or the overlay lists ``*``."""
        return repo in self.repos or "*" in self.repos

    def repo_in_scope(self, repo: str) -> bool:
        """A repository holds in-scope work when it is in scope whole or any in-scope pull request lives in it."""
        return self.repo_whole(repo) or any(
            ref.partition("#")[0] == repo for ref in (*self.prs, *self.prerequisites)
        )

    def pr_scope(self, ref: str) -> str:
        """``in-scope`` for a listed pull request or one of an in-scope repository, ``paused`` for a drafted one, else ``unlisted``."""
        repo = ref.partition("#")[0]
        if ref in self.prs or ref in self.prerequisites or self.repo_whole(repo):
            return IN_SCOPE
        return PAUSED if ref in self.paused else UNLISTED


__all__ = ["IN_SCOPE", "PAUSED", "UNLISTED", "ModelGoalScope"]
