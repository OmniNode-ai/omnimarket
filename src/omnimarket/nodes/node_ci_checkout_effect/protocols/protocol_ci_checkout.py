# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocols of node_ci_checkout_effect."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolCheckoutGit(Protocol):
    """Answers whether a local git source holds a commit."""

    def has_commit(self, repo_path: str, sha: str) -> bool: ...


@runtime_checkable
class ProtocolCloneSync(Protocol):
    """Syncs a host clone; True when the sync command exited 0."""

    def sync(self, repo: str, ref: str | None) -> bool: ...


__all__ = ["ProtocolCheckoutGit", "ProtocolCloneSync"]
