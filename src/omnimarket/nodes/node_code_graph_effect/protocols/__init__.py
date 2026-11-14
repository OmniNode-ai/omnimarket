# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_code_graph_effect: where a repository's files at a ref come from."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Protocol

from omnimarket.models.model_code_graph import (
    ModelRepoSnapshot,
    ModelShadowPullRequest,
)
from omnimarket.models.model_code_graph_lookup import ModelCodeGraphVersion


class RepoSourceError(RuntimeError):
    """A repository or ref could not be read. Never an empty answer."""


class ProtocolRepoSource(Protocol):
    """Read the files the graph wants from one repository at one ref."""

    def snapshot(
        self, repo: str, ref: str, wanted: Callable[[str], bool]
    ) -> ModelRepoSnapshot:
        """The files whose repository-relative path ``wanted`` accepts, at ``ref``.

        Raises :class:`RepoSourceError` when the repository or the ref cannot be read.
        """
        ...


class PullRequestSourceError(RuntimeError):
    """The open pull requests could not be read. Never an empty answer."""


class ProtocolPullRequestSource(Protocol):
    """The open pull requests of one repository."""

    def open_pull_requests(self, repo: str) -> tuple[ModelShadowPullRequest, ...]:
        """Newest first. Raises :class:`PullRequestSourceError` when they cannot be read."""
        ...


class ProtocolCodeGraphVersionStore(Protocol):
    """The stored versions of the graph, keyed by repository and commit."""

    def put(self, version: ModelCodeGraphVersion) -> tuple[bool, tuple[str, ...]]:
        """Store a version; ``(written, pruned)``.

        A version already stored for the repository and commit is not stored twice (``written`` is
        false). ``pruned`` are the commits dropped for being past the retention.
        """
        ...

    def get(self, repo: str, commit: str) -> ModelCodeGraphVersion | None:
        """The version stored for the repository and commit, or none."""
        ...

    def newest(self, repo: str) -> ModelCodeGraphVersion | None:
        """The head: the version stored last for the repository, or none."""
        ...

    def versions(self, repo: str) -> tuple[ModelCodeGraphVersion, ...]:
        """Every stored version of the repository, oldest first."""
        ...


class ProtocolCommitClock(Protocol):
    """When a commit was made, read from the repository's clone."""

    def committed_at(self, repo: str, commit: str) -> datetime | None:
        """The commit's time, or none when the clone does not hold the commit."""
        ...


__all__ = [
    "ProtocolCodeGraphVersionStore",
    "ProtocolCommitClock",
    "ProtocolPullRequestSource",
    "ProtocolRepoSource",
    "PullRequestSourceError",
    "RepoSourceError",
]
