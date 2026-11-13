# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Where piece 9's landing snapshot comes from (OMN-22918).

``WatcherFileLandingSource`` reads the pull request watcher's state file and the landing goal's scope
overlay, and counts what the goal counts through the same reader the floor uses, so the reading and the
floor's numbers cannot drift apart. A state file or scope that cannot be read is an error, never an empty
queue: an unread scope puts nothing in scope, and an empty queue would read WORKING. ``InMemoryLandingSource``
answers a fixed snapshot or error and is the default of the tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import httpx

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
    resolve_setting,
)
from omnimarket.models.floor_17a.model_floor_17a import ModelGoalScope
from omnimarket.models.model_reading import (
    ModelLandingHead,
    ModelLandingRequirements,
    ModelLandingSnapshot,
)
from omnimarket.models.pr_state.model_pr_state_page_fetch import (
    ModelPrStatePageRequest,
)
from omnimarket.models.pr_state.model_pr_state_projection_page import (
    ModelPrStateProjectionPage,
    ModelPrStateProjectionRefusal,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.sources import read_watcher
from omnimarket.nodes.node_pr_state_read_effect.handlers.handler_pr_state_page_source import (
    UNREACHABLE,
    HandlerPrStatePageSource,
    open_projection_client,
)
from omnimarket.nodes.node_pr_state_read_effect.policy import (
    load_pr_state_read_policy,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelLandingProjectionRoute,
    ModelLandingRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import ModelLandingRead


class InMemoryLandingSource:
    """A fixed answer: the in-memory backend of the landing source."""

    def __init__(
        self,
        snapshot: ModelLandingSnapshot | None = None,
        *,
        error: str | None = None,
    ) -> None:
        if (snapshot is None) == (error is None):
            raise ValueError("an in-memory landing source holds a snapshot or an error")
        self._answer = ModelLandingRead(snapshot=snapshot, error=error)

    def read(
        self, requirements: ModelLandingRequirements, now: datetime
    ) -> ModelLandingRead:
        return self._answer


class WatcherFileLandingSource:
    """The real backend: the watcher's state file and the goal scope overlay the route names."""

    def __init__(self, route: ModelLandingRoute, environ: Mapping[str, str]) -> None:
        self._route = route
        self._environ = environ

    def read(
        self, requirements: ModelLandingRequirements, now: datetime
    ) -> ModelLandingRead:
        path_text = self._environ.get(self._route.watcher_state_env, "").strip()
        if not path_text:
            return ModelLandingRead(error=f"{self._route.watcher_state_env} is not set")
        try:
            scope = load_structured_overlay(
                self._route.goal_scope_overlay_id,
                ModelGoalScope,
                self._environ,
                file_env=self._route.goal_scope_file_env,
            )
        except NodeOverlayError as exc:
            return ModelLandingRead(
                error=f"the goal scope overlay is not read ({type(exc).__name__})"
            )
        now = now.astimezone(UTC)
        watcher = read_watcher(
            Path(path_text),
            now,
            excluded_classes=(),
            queue_classes=self._route.queue_classes,
            merge_window_minutes=requirements.merge_window_seconds // 60,
            scope=scope,
            conflict_states=self._route.conflict_merge_states,
        )
        if watcher is None:
            return ModelLandingRead(error="the watcher state file is not read")
        if not watcher.scoped or watcher.merges_in_window is None:
            return ModelLandingRead(error="the landing goal scope is not read")
        heads = tuple(
            ModelLandingHead(
                ref=ref,
                waiting_seconds=minutes * 60,
                conflicted=ref in watcher.queue_conflicted,
            )
            for ref, minutes in watcher.queue_ages
        )
        return ModelLandingRead(
            snapshot=ModelLandingSnapshot(
                age_seconds=max(int((now - watcher.last_tick).total_seconds()), 0),
                newest_at=watcher.last_tick,
                heads=heads,
                merges_in_window=watcher.merges_in_window,
            )
        )


PR_STATE_NODE = "node_pr_state_read_effect"
PROJECTION_API_URL_SETTING = "projection_api_url"


class ProjectionLandingSource:
    """The lab backend: pull request state read from the pr_state projection through the pr_state read node.

    The projection address is the ``projection_api_url`` of the ``node_pr_state_read_effect`` overlay and nothing
    else: no environment variable shadows it and no address is written here. The page comes from the read node's
    own page source (``GET /projection/{topic}``); no GitHub request is made.

    What a projection row can and cannot say, against the watcher's state file:

    * ``age_seconds`` is the age of the newest ``observed_at`` in the page: the projection's freshness. A projection
      nothing has written to for longer than the requirement's bound reads stale, as a stopped watcher does.
    * ``waiting_seconds`` is the age of the row's ``ci_read_at``. The row carries no time at which CI turned green, so
      this is the age of the last CI read of a green head: a lower bound on how long it has waited.
    * ``conflicted`` is never set: the row carries no merge state.
    * A page the projection reports ``truncated`` is read as it is: rows come newest first, so the merges of the
      window are in it, and a head older than the page is not.

    Every read that cannot be completed is an error, never an empty queue. The runtime that serves the projection
    restarts on every deploy and the address refuses connections until it is back: a projection that could not be
    reached (no answer, or a 5xx) is read again, by the passes and pauses the route's ``retry`` declares, and the
    error is returned only when the last pass failed too (OMN-23077). A refusal of any other kind is not transient and
    is returned at once.
    """

    def __init__(
        self,
        route: ModelLandingProjectionRoute,
        environ: Mapping[str, str],
        *,
        client_factory: Callable[[str], httpx.Client] = open_projection_client,
        pause: Callable[[float], None] = time.sleep,
    ) -> None:
        self._route = route
        self._environ = environ
        self._client_factory = client_factory
        self._pause = pause

    def _fetch(
        self, url: str
    ) -> ModelPrStateProjectionPage | ModelPrStateProjectionRefusal:
        client = self._client_factory(url)
        try:
            return (
                HandlerPrStatePageSource(client)
                .handle(
                    ModelPrStatePageRequest(
                        topic=load_pr_state_read_policy().projection_topic
                    )
                )
                .result
            )
        finally:
            client.close()

    def _fetch_with_retries(
        self, url: str
    ) -> tuple[ModelPrStateProjectionPage | ModelPrStateProjectionRefusal, int]:
        """The page or the last refusal, and the number of reads it took."""
        fetched = self._fetch(url)
        reads = 1
        retry = self._route.retry
        if retry is None:
            return fetched, reads
        for _ in range(retry.passes):
            if not _is_transient(fetched):
                break
            self._pause(retry.pause_seconds)
            fetched = self._fetch(url)
            reads += 1
        return fetched, reads

    def read(
        self, requirements: ModelLandingRequirements, now: datetime
    ) -> ModelLandingRead:
        try:
            url = resolve_setting(
                PR_STATE_NODE,
                PROJECTION_API_URL_SETTING,
                fields=frozenset({PROJECTION_API_URL_SETTING}),
                environ=self._environ,
            )
            scope = load_structured_overlay(
                self._route.goal_scope_overlay_id,
                ModelGoalScope,
                self._environ,
                file_env=self._route.goal_scope_file_env,
            )
        except NodeOverlayError as exc:
            return ModelLandingRead(
                error=f"the projection address or goal scope is not read ({type(exc).__name__})"
            )
        fetched, reads = self._fetch_with_retries(url)
        if isinstance(fetched, ModelPrStateProjectionRefusal):
            after = f" after {reads} reads" if reads > 1 else ""
            return ModelLandingRead(
                error=f"the pr_state projection is not read{after} ({fetched.error})"
            )
        if not fetched.rows:
            return ModelLandingRead(error="the pr_state projection holds no rows")
        now = now.astimezone(UTC)
        newest = max(row.observed_at for row in fetched.rows)
        cutoff = now.timestamp() - requirements.merge_window_seconds
        heads: list[ModelLandingHead] = []
        merges = 0
        for row in fetched.rows:
            ref = f"{row.repo}#{row.pr_number}"
            where = scope.classify(ref, row.title, row.head_ref)
            if where != "in_scope":
                continue
            if row.state == "merged":
                merged_at = _parse_stamp(row.merged_at)
                if merged_at is not None and merged_at.timestamp() >= cutoff:
                    merges += 1
            elif (
                row.state == "open"
                and not row.draft
                and row.watcher_class in self._route.queue_classes
            ):
                read_at = _parse_stamp(row.ci_read_at)
                if read_at is None:
                    continue
                heads.append(
                    ModelLandingHead(
                        ref=ref,
                        waiting_seconds=max(int((now - read_at).total_seconds()), 0),
                    )
                )
        return ModelLandingRead(
            snapshot=ModelLandingSnapshot(
                age_seconds=max(int((now - newest).total_seconds()), 0),
                heads=tuple(sorted(heads, key=lambda h: -h.waiting_seconds)),
                merges_in_window=merges,
            )
        )


def _is_transient(
    fetched: ModelPrStateProjectionPage | ModelPrStateProjectionRefusal,
) -> bool:
    """A refusal that a restarting runtime gives: no answer at all, or a server error."""
    return isinstance(fetched, ModelPrStateProjectionRefusal) and (
        fetched.error == UNREACHABLE or fetched.status_code >= 500
    )


def _parse_stamp(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


__all__ = [
    "InMemoryLandingSource",
    "ProjectionLandingSource",
    "WatcherFileLandingSource",
]
