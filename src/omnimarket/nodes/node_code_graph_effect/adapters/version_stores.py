# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory version store and commit clocks of the lookup (OMN-23648).

The store keeps the newest ``retention_versions`` versions of each repository in the order they were
stored, so the head is the version stored last and a prune drops the oldest. The Postgres-backed
projection of the same port is a later slice of the ticket.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from omnimarket.models.model_code_graph_lookup import ModelCodeGraphVersion


class InMemoryVersionStore:
    """Versions by repository and commit; retention is how many of a repository stay."""

    def __init__(self, retention_versions: int) -> None:
        if retention_versions < 1:
            raise ValueError("retention_versions is at least 1")
        self._retention = retention_versions
        #: Per repository, versions oldest first.
        self._by_repo: dict[str, list[ModelCodeGraphVersion]] = {}

    def put(self, version: ModelCodeGraphVersion) -> tuple[bool, tuple[str, ...]]:
        kept = self._by_repo.setdefault(version.repo, [])
        if any(stored.commit == version.commit for stored in kept):
            return False, ()
        kept.append(version)
        pruned = tuple(stored.commit for stored in kept[: -self._retention])
        del kept[: -self._retention]
        return True, pruned

    def get(self, repo: str, commit: str) -> ModelCodeGraphVersion | None:
        return next(
            (v for v in self._by_repo.get(repo, ()) if v.commit == commit), None
        )

    def newest(self, repo: str) -> ModelCodeGraphVersion | None:
        kept = self._by_repo.get(repo)
        return kept[-1] if kept else None

    def versions(self, repo: str) -> tuple[ModelCodeGraphVersion, ...]:
        return tuple(self._by_repo.get(repo, ()))


class InMemoryCommitClock:
    """Commit times from a mapping of ``(repo, commit)``; a commit it does not hold is none."""

    def __init__(self, times: Mapping[tuple[str, str], datetime]) -> None:
        self._times = times

    def committed_at(self, repo: str, commit: str) -> datetime | None:
        return self._times.get((repo, commit))
