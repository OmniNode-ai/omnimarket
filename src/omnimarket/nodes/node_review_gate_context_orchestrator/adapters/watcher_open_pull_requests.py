# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The open pull requests of the sweep, read from the PR watcher's state JSON.

The collector turns each open pull request the watcher last saw into the subject the review-gate
context decision judges: its repository as ``owner/name`` with the overlay's owner, its head, its
author and its head ref. Nothing is read from GitHub; the watcher's state is the read. The class
reads no file: ``load_watcher_state`` reads the JSON and the caller hands the dict over.

A state whose ``last_tick`` is older than the overlay's bound is stale: the sweep reads nothing,
because an old state would decide heads the watcher has since moved. A PR whose facts lack a
readable repository, number, head or author is skipped and counted in ``skipped``, never decided.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.models.model_landing_review import SHA_PATTERN
from omnimarket.models.model_review_gate_context import ModelReviewGatePrSubject
from omnimarket.nodes.node_review_gate_context_orchestrator.overlay import (
    ModelReviewGateSweepSettings,
)
from omnimarket.nodes.node_review_gate_context_orchestrator.protocols.protocol_review_gate_sources import (
    ReviewGateSourceError,
)


def load_watcher_state(path: Path) -> dict[str, Any]:
    """The PR watcher's state JSON dict at ``path``."""
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ReviewGateSourceError("the watcher state JSON is not an object")
    return loaded


def _tick(text: object) -> datetime:
    """The watcher's ``last_tick`` as a timezone-aware instant."""
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewGateSourceError("watcher last_tick is not a timestamp") from exc
    if parsed.tzinfo is None:
        raise ReviewGateSourceError("watcher last_tick carries no zone")
    return parsed


class WatcherOpenPullRequests:
    """The open pull requests over the PR watcher's state JSON dict."""

    def __init__(
        self,
        state: Mapping[str, Any],
        settings: ModelReviewGateSweepSettings,
        now: datetime,
    ) -> None:
        self._state = state
        self._settings = settings
        self._now = now
        self.skipped = 0

    def open_pull_requests(
        self, repositories: tuple[str, ...]
    ) -> tuple[ModelReviewGatePrSubject, ...]:
        """The open, non-draft pull requests of the requested repositories."""
        prs = self._prs()
        last_tick = self._state.get("last_tick")
        if last_tick is None:
            raise ReviewGateSourceError("the watcher state holds no last_tick")
        age = (self._now - _tick(last_tick)).total_seconds()
        if age > self._settings.max_age_seconds:
            raise ReviewGateSourceError(
                f"the watcher state is {int(age)} s old, past the "
                f"{self._settings.max_age_seconds} s bound"
            )
        owner = self._settings.owner
        wanted = self._wanted(repositories)
        subjects: list[ModelReviewGatePrSubject] = []
        for record in prs.values():
            subject = self._subject(record, owner, wanted)
            if subject is not None:
                subjects.append(subject)
        return tuple(subjects)

    def _prs(self) -> dict[str, Any]:
        prs = self._state.get("prs")
        if not isinstance(prs, dict):
            raise ReviewGateSourceError("the watcher state holds no prs table")
        return prs

    def _wanted(self, repositories: tuple[str, ...]) -> set[str]:
        """The repositories the sweep covers, bare and as ``owner/name``."""
        named = repositories or self._settings.repositories
        owner = self._settings.owner
        return {*named, *(f"{owner}/{name}" for name in named)}

    def _subject(
        self, record: Any, owner: str, wanted: set[str]
    ) -> ModelReviewGatePrSubject | None:
        """One PR record as a subject, or None when the sweep does not decide it."""
        facts = record.get("facts") if isinstance(record, Mapping) else None
        if not isinstance(facts, Mapping):
            self.skipped += 1
            return None
        repo = facts.get("repo")
        number = facts.get("number")
        head_sha = facts.get("head_sha")
        author = facts.get("author")
        readable = (
            isinstance(repo, str)
            and repo.strip() != ""
            and isinstance(number, int)
            and not isinstance(number, bool)
            and number >= 1
            and isinstance(head_sha, str)
            and re.fullmatch(SHA_PATTERN, head_sha) is not None
            and isinstance(author, str)
            and author.strip() != ""
        )
        if not readable:
            self.skipped += 1
            return None
        if f"{owner}/{repo}" not in wanted and repo not in wanted:
            return None
        if str(facts.get("state", "")).upper() != "OPEN":
            return None
        if facts.get("draft") is True and not self._settings.include_drafts:
            return None
        return ModelReviewGatePrSubject(
            repository=f"{owner}/{repo}",
            pr_number=number,
            head_sha=head_sha,
            author_login=author,
            head_ref=str(facts.get("head_ref", "")),
            title=str(facts.get("title", "")),
            review=None,
        )


__all__ = ["WatcherOpenPullRequests", "load_watcher_state"]
