# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The sources of the merged-change reading: the change to node map, the build facts and the runtime facts."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
)


@runtime_checkable
class ProtocolMergedChangeSources(Protocol):
    """Every read of one reading. A source that cannot read says so in the value it returns, never by an empty one."""

    def resolve_changes(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[ModelMcrChange, ...]:
        """The given changes with their merge sha filled in where the source can find it."""
        ...

    def read_runners(self) -> tuple[ModelMcrRunner, ...]:
        """The declared change to node or unit map."""
        ...

    def read_build(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[tuple[ModelMcrBuildFact, ...], tuple[str, ...], datetime | None]:
        """Build facts, the repositories not read, and when the running build started (None: not read)."""
        ...

    def read_runtime(self, runners: Sequence[ModelMcrRunner]) -> ModelMcrRuntimeFacts:
        """Loaded nodes, subscriptions, published results and unit runs, with the groups not read named."""
        ...


@runtime_checkable
class ProtocolMergedChangeRuntimeReader(Protocol):
    """The reads that need the runtime host: its container and units, and its checkouts."""

    def read(
        self, runners: Sequence[ModelMcrRunner]
    ) -> tuple[ModelMcrRuntimeFacts, datetime | None]:
        """The runtime facts and when the running build started (None: not read)."""
        ...

    def checkout_ancestry(
        self, path: str, shas: Sequence[str]
    ) -> dict[str, bool | None]:
        """Whether each sha is in the history of the checkout's head on the runtime host (None: it could not say)."""
        ...
