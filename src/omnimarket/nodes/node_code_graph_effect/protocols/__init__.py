# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_code_graph_effect: where a repository's files at a ref come from."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from omnimarket.models.model_code_graph import ModelRepoSnapshot


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


__all__ = ["ProtocolRepoSource", "RepoSourceError"]
