# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The in-memory profile of the merged-change reading's sources: every fact is handed in, nothing is read."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from omnimarket.models.model_merged_change_reading import (
    ModelMcrBuildFact,
    ModelMcrChange,
    ModelMcrRunner,
    ModelMcrRuntimeFacts,
)


class SourceInMemoryChangeReading:
    """``ProtocolMergedChangeSources`` over values: the default profile of tests and of a run with no runtime host."""

    def __init__(
        self,
        *,
        runners: Sequence[ModelMcrRunner] = (),
        build_facts: Sequence[ModelMcrBuildFact] = (),
        build_unread: Sequence[str] = (),
        build_started_at: datetime | None = None,
        runtime: ModelMcrRuntimeFacts | None = None,
        shas: dict[tuple[str, int], str] | None = None,
    ) -> None:
        self._runners = tuple(runners)
        self._build_facts = tuple(build_facts)
        self._build_unread = tuple(build_unread)
        self._started = build_started_at
        self._runtime = runtime or ModelMcrRuntimeFacts()
        self._shas = shas or {}

    def resolve_changes(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[ModelMcrChange, ...]:
        return tuple(
            c
            if c.merge_sha
            else c.model_copy(
                update={"merge_sha": self._shas.get((c.repo, c.number), "")}
            )
            for c in changes
        )

    def read_runners(self) -> tuple[ModelMcrRunner, ...]:
        return self._runners

    def read_build(
        self, changes: Sequence[ModelMcrChange]
    ) -> tuple[tuple[ModelMcrBuildFact, ...], tuple[str, ...], datetime | None]:
        return self._build_facts, self._build_unread, self._started

    def read_runtime(self, runners: Sequence[ModelMcrRunner]) -> ModelMcrRuntimeFacts:
        return self._runtime
