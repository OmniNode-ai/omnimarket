# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two backends of the pull request source: in memory, and the pull request watcher's state file.

The watcher state is the lane-side read of GitHub (a lane never reads GitHub itself): one entry per
pull request with its repository, number, state, base branch and exact head commit.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.model_code_graph import ModelShadowPullRequest
from omnimarket.nodes.node_code_graph_effect.protocols import PullRequestSourceError

_REMOTE = "origin/"


class InMemoryPullRequestSource:
    """Pull requests given at construction; ``error`` injects an unreadable source."""

    def __init__(
        self,
        pull_requests: Sequence[ModelShadowPullRequest] = (),
        *,
        error: str | None = None,
    ) -> None:
        self._pull_requests = tuple(pull_requests)
        self._error = error

    def open_pull_requests(self, repo: str) -> tuple[ModelShadowPullRequest, ...]:
        if self._error is not None:
            raise PullRequestSourceError(self._error)
        found = [pr for pr in self._pull_requests if pr.repo == repo]
        return tuple(sorted(found, key=lambda pr: pr.number, reverse=True))


class WatcherStatePullRequestSource:
    """Open pull requests read from the watcher's state file, newest number first.

    With ``max_age`` the state's own ``last_tick`` must be at most that old at ``clock()``: a stopped
    watcher leaves valid JSON behind, and a reading built from it would carry today's time over an
    old inventory.
    """

    def __init__(
        self,
        path: Path,
        *,
        max_age: timedelta | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_age is not None and clock is None:
            raise ValueError("a max_age bound needs the clock it is read against")
        self._path = path
        self._max_age = max_age
        self._clock = clock

    def _document(self) -> dict[str, object]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PullRequestSourceError(
                f"watcher-state-unreadable:{type(exc).__name__}"
            ) from None
        if not isinstance(document, dict) or not isinstance(document.get("prs"), dict):
            raise PullRequestSourceError("watcher-state-has-no-prs")
        self._check_fresh(document)
        return document

    def _check_fresh(self, document: dict[str, object]) -> None:
        if self._max_age is None or self._clock is None:
            return
        tick = document.get("last_tick")
        try:
            read_at = datetime.fromisoformat(str(tick).replace("Z", "+00:00"))
        except ValueError:
            raise PullRequestSourceError("watcher-state-stale:no-last-tick") from None
        if read_at.tzinfo is None:
            raise PullRequestSourceError("watcher-state-stale:no-last-tick")
        if self._clock().astimezone(UTC) - read_at > self._max_age:
            raise PullRequestSourceError(f"watcher-state-stale:{tick}")

    def open_pull_requests(self, repo: str) -> tuple[ModelShadowPullRequest, ...]:
        prs = self._document()["prs"]
        assert isinstance(prs, dict)
        found: list[ModelShadowPullRequest] = []
        for entry in prs.values():
            facts = entry.get("facts") if isinstance(entry, dict) else None
            if not isinstance(facts, dict):
                # An entry that names no facts cannot be told apart from one of this repository.
                raise PullRequestSourceError("watcher-entry-incomplete:no-facts")
            if facts.get("repo") != repo:
                continue
            if facts.get("state") != "OPEN":
                continue
            base, head_ref, head_sha, number = (
                facts.get("base"),
                facts.get("head_ref"),
                facts.get("head_sha"),
                facts.get("number"),
            )
            if not (
                isinstance(base, str)
                and isinstance(head_ref, str)
                and isinstance(head_sha, str)
                and isinstance(number, int)
                and not isinstance(number, bool)
                and number >= 1
                and base
                and head_ref
                and head_sha
            ):
                raise PullRequestSourceError(f"watcher-entry-incomplete:{repo}")
            found.append(
                ModelShadowPullRequest(
                    repo=repo,
                    number=number,
                    base_ref=f"{_REMOTE}{base}",
                    head_ref=head_ref,
                    head_sha=head_sha,
                )
            )
        return tuple(sorted(found, key=lambda pr: pr.number, reverse=True))
