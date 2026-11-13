# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One concrete event model per routing entry, so the runtime's typed arm resolves each entry.

The runtime resolver returns None for a union-annotated handle(), so the original
HandlerMergeOverrideAlarm is wired as a raw-dict projection arm. Each entry below
annotates its handle() with one concrete pydantic model and delegates to the
unchanged HandlerMergeOverrideAlarm by composition. Every handle() returns None:
the node publishes its own events through its event_publisher, and a returned
model would be published by the runtime to the first entry of publish_topics.
"""

from __future__ import annotations

import logging
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
    ModelMergeOverrideInput,
    ModelMergeOverrideSweepTrigger,
)
from omnimarket.nodes.node_merge_override_alarm_effect.protocols.protocol_override_row_reader import (
    ProtocolOverrideRowReader,
)
from omnimarket.protocols.protocol_projection_database_sync import DatabaseAdapter

logger = logging.getLogger(__name__)


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

    def _run(self, request: ModelMergeOverrideInput) -> None:
        result = self._core.handle(request)
        logger.info(
            "merge override alarm handled %s: %s",
            type(request).__name__,
            result.outcome.value,
        )
        if result.error is not None:
            logger.warning("merge override alarm error: %s", result.error)


class HandlerMergeOverridePrMerged(_HandlerMergeOverrideEntry):
    """A merge from the Actions publisher: one verdict per pull request."""

    def handle(self, request: ModelPrMergedEvent) -> None:
        self._run(request)


class HandlerMergeOverrideRepoMerged(_HandlerMergeOverrideEntry):
    """The same merge from the PR watcher: the same verdict, not a second one."""

    def handle(self, request: ModelRepoMerged) -> None:
        self._run(request)


class HandlerMergeOverrideLandingCompleted(_HandlerMergeOverrideEntry):
    """A landing completion: a receipt, or the answer to a pending merge read."""

    def handle(self, request: ModelPrLandingGithubCompleted) -> None:
        self._run(request)


class HandlerMergeOverrideLandingFailed(_HandlerMergeOverrideEntry):
    """A refused or failed read of a pending merge."""

    def handle(self, request: ModelPrLandingGithubFailed) -> None:
        self._run(request)


class HandlerMergeOverrideSweep(_HandlerMergeOverrideEntry):
    """The runtime heartbeat that sweeps reads unanswered past the overlay's timeout."""

    def handle(self, request: ModelMergeOverrideSweepTrigger) -> None:
        self._run(request)


__all__ = [
    "HandlerMergeOverrideLandingCompleted",
    "HandlerMergeOverrideLandingFailed",
    "HandlerMergeOverridePrMerged",
    "HandlerMergeOverrideRepoMerged",
    "HandlerMergeOverrideSweep",
]
