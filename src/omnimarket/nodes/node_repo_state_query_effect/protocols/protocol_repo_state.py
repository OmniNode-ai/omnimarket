# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four dependencies the repository state handler is given (OMN-23033).

None of them is constructed inside the handler: the serve process builds the deployed
adapters from the node's overlay, and the tests give planted ones.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from omnimarket.nodes.node_repo_state_query_effect.models.model_repo_state_query import (
    EnumRepoStateMirror,
    ModelBusPrEvent,
    ModelBusRepoHead,
    ModelMirrorPull,
    ModelRepoStateFile,
)


class ProtocolRepoCloneMirror(Protocol):
    """The host's canonical clone of a repository, read with local git only."""

    def default_branch(self, repo: str) -> str | None: ...

    def refs(self, repo: str) -> dict[str, str] | None: ...

    def contains(self, repo: str, branch: str, sha: str) -> bool: ...

    def resolve(self, repo: str, revision: str) -> str | None: ...

    def read_files(
        self, repo: str, sha: str, paths: tuple[str, ...]
    ) -> tuple[ModelRepoStateFile, ...]: ...


class ProtocolPrWatcherMirror(Protocol):
    """The PR watcher's state on this host."""

    def read_at(self) -> datetime | None: ...

    def record(self, repo: str, pr_number: int) -> dict[str, Any] | None: ...


class ProtocolBusRepoState(Protocol):
    """The latest repository state the bus has carried (merge and push events)."""

    def head(self, repo: str, branch: str) -> ModelBusRepoHead | None: ...

    def heads(self, repo: str) -> tuple[ModelBusRepoHead, ...]: ...

    def pr_event(self, repo: str, pr_number: int) -> ModelBusPrEvent | None: ...


class ProtocolMirrorPuller(Protocol):
    """Bring one mirror of one repository up to the bus's state."""

    def pull(
        self, repo: str, mirror: EnumRepoStateMirror, target_sha: str
    ) -> ModelMirrorPull: ...


__all__ = [
    "ProtocolBusRepoState",
    "ProtocolMirrorPuller",
    "ProtocolPrWatcherMirror",
    "ProtocolRepoCloneMirror",
]
