# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Protocol for GitHub review thread operations — injected so tests can mock cleanly."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolGitHubReviewClient(Protocol):
    """Minimal interface for GitHub review thread mutations."""

    def unresolve_thread(self, thread_id: str) -> bool:
        """Call the GraphQL resolveReviewThread mutation with unresolve=true.

        Returns True on success, raises on unrecoverable error.
        """
        ...

    def post_comment(self, pr_node_id: str, body: str) -> bool:
        """Post a comment on the pull request identified by pr_node_id.

        Returns True on success, raises on unrecoverable error.
        """
        ...


__all__: list[str] = ["ProtocolGitHubReviewClient"]
