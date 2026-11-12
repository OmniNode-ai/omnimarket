# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One concrete event model per routing entry, so the runtime's typed arm resolves each entry.

The runtime resolver returns None for a union-annotated handle(), so the original
HandlerMergeOverrideAlarm is wired as a raw-dict projection arm. Each entry below
annotates its handle() with one concrete pydantic model and delegates to the
unchanged HandlerMergeOverrideAlarm by composition.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from omnimarket.events.github import ModelPrMergedEvent
from omnimarket.events.pr_landing_github.model_pr_landing_github_completed import (
    ModelPrLandingGithubCompleted,
)
from omnimarket.events.pr_landing_github.model_pr_landing_github_failed import (
    ModelPrLandingGithubFailed,
)
from omnimarket.models.model_repo_merged import ModelRepoMerged
from omnimarket.nodes.node_merge_override_alarm_effect.handlers.handler_merge_override_alarm import (
    HandlerMergeOverrideAlarm,
)
from omnimarket.nodes.node_merge_override_alarm_effect.models import (
    ModelMergeOverrideResult,
    ModelMergeOverrideSweepTrigger,
)
from omnimarket.nodes.node_merge_override_alarm_effect.protocols.protocol_override_row_reader import (
    ProtocolOverrideRowReader,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter


class _HandlerMergeOverrideEntry:
    """Base for the five routing entries: one concrete model, one delegate."""

    def __init__(
        self,
        *,
        event_publisher: Callable[[str, bytes], None] | None = None,
        database: DatabaseAdapter | None = None,
        reader: ProtocolOverrideRowReader | None = None,
        clock: Callable[[], float] | None = None,
        contract_path: Path | None = None,
    ) -> None:
        self._core = HandlerMergeOverrideAlarm(
            event_publisher=event_publisher,
            database=database,
            reader=reader,
            clock=clock,
            contract_path=contract_path,
        )


class HandlerMergeOverridePrMerged(_HandlerMergeOverrideEntry):
    """A merge from the Actions publisher: one verdict per pull request."""

    def handle(self, request: ModelPrMergedEvent) -> ModelMergeOverrideResult:
        return self._core.handle(request)


class HandlerMergeOverrideRepoMerged(_HandlerMergeOverrideEntry):
    """The same merge from the PR watcher: the same verdict, not a second one."""

    def handle(self, request: ModelRepoMerged) -> ModelMergeOverrideResult:
        return self._core.handle(request)


class HandlerMergeOverrideLandingCompleted(_HandlerMergeOverrideEntry):
    """A landing completion: a receipt, or the answer to a pending merge read."""

    def handle(
        self, request: ModelPrLandingGithubCompleted
    ) -> ModelMergeOverrideResult:
        return self._core.handle(request)


class HandlerMergeOverrideLandingFailed(_HandlerMergeOverrideEntry):
    """A refused or failed read of a pending merge."""

    def handle(self, request: ModelPrLandingGithubFailed) -> ModelMergeOverrideResult:
        return self._core.handle(request)


class HandlerMergeOverrideSweep(_HandlerMergeOverrideEntry):
    """The runtime heartbeat that sweeps reads unanswered past the overlay's timeout."""

    def handle(
        self, request: ModelMergeOverrideSweepTrigger
    ) -> ModelMergeOverrideResult:
        return self._core.handle(request)


__all__ = [
    "HandlerMergeOverrideLandingCompleted",
    "HandlerMergeOverrideLandingFailed",
    "HandlerMergeOverridePrMerged",
    "HandlerMergeOverrideRepoMerged",
    "HandlerMergeOverrideSweep",
]
