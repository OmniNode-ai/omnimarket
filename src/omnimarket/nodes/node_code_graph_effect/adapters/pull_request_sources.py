# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two backends of the pull request source: in memory, and the pull request watcher's state file.

The watcher state is the lane-side read of GitHub (a lane never reads GitHub itself): one entry per
pull request with its repository, number, state, base branch and exact head commit.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
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
    """Open pull requests read from the watcher's state file, newest number first."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _document(self) -> dict[str, object]:
        try:
            document = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PullRequestSourceError(
                f"watcher-state-unreadable:{type(exc).__name__}"
            ) from None
        if not isinstance(document, dict) or not isinstance(document.get("prs"), dict):
            raise PullRequestSourceError("watcher-state-has-no-prs")
        return document

    def open_pull_requests(self, repo: str) -> tuple[ModelShadowPullRequest, ...]:
        prs = self._document()["prs"]
        assert isinstance(prs, dict)
        found: list[ModelShadowPullRequest] = []
        for entry in prs.values():
            facts = entry.get("facts") if isinstance(entry, dict) else None
            if not isinstance(facts, dict) or facts.get("repo") != repo:
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
