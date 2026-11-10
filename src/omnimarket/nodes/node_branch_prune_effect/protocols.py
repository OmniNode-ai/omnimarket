# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The two boundaries the prune node takes by injection."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_branch_prune_effect.models import (
    ModelBranchInfo,
    ModelPullRequestInfo,
)


class ProtocolBranchHost(Protocol):
    """A source-control host holding remote branches and pull requests."""

    def default_branch(self, repository: str) -> str: ...

    def list_branches(self, repository: str) -> list[ModelBranchInfo]: ...

    def list_pull_requests(self, repository: str) -> list[ModelPullRequestInfo]:
        """Every pull request, any state, with the sha its head had last."""
        ...

    def delete_branch(self, repository: str, branch: str, *, expected_sha: str) -> None:
        """Delete the branch only while it still points at ``expected_sha``."""
        ...


class ProtocolTipRecord(Protocol):
    """Durable record of branch tips, written before a delete."""

    def record(self, repository: str, branch: str, tip_sha: str) -> None:
        """Persist one tip. Raise when the write did not land."""
        ...
