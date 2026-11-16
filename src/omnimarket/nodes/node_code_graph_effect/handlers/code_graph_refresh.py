# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh on merge: rebuild the graph only when a repository head moved.

The nightly build runs once a day, so a merge can wait that long to reach the graph. The refresh
reads the heads of the last build from the state directory's ``code-graph-built.json``, reads each
declared repository's head now, and runs the writing build only when some head moved and neither the
last build nor the last attempt is less than :data:`REFRESH_MIN_INTERVAL` old, so a burst of merges
builds once instead of once per tick and a failing build is not rerun every tick. Unchanged heads are
``fresh`` and append nothing; a too-young build or attempt, or a build another run holds the state
directory's lock for, is ``deferred``; a head that cannot be read, a build that fails, or a build
that leaves no rewritten marker is ``failed`` - the refresh is never fresh when it cannot prove the
heads unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.model_code_graph import (
    EnumRefreshOutcome,
    ModelCodeGraphRefreshResult,
    ModelCodeGraphResult,
)
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_state import (
    BUILT_MARKER,
    build_lock,
    read_attempt,
    read_marker,
    record_attempt,
)
from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphOverlay
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolRepoHeads,
    RepoSourceError,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)

#: A build or attempt younger than this defers the refresh, so a burst of merges builds once.
REFRESH_MIN_INTERVAL = timedelta(minutes=15)

__all__ = ["BUILT_MARKER", "REFRESH_MIN_INTERVAL", "CodeGraphRefresh"]


class CodeGraphRefresh:
    """Compare the declared repositories' heads with the last build's, and build when one moved."""

    def __init__(
        self,
        *,
        overlay: ModelCodeGraphOverlay,
        heads: ProtocolRepoHeads,
        state_directory: Path,
        run: Callable[[], ModelCodeGraphResult],
        clock: Callable[[], datetime],
        min_interval: timedelta = REFRESH_MIN_INTERVAL,
    ) -> None:
        self._overlay = overlay
        self._heads = heads
        self._state_directory = state_directory
        self._run = run
        self._clock = clock
        self._min_interval = min_interval

    def handle(self) -> ModelCodeGraphRefreshResult:
        """Deferred while another build holds the lock, fresh when no head moved, else build."""
        with build_lock(self._state_directory) as held:
            if not held:
                built_at, built_heads = read_marker(self._state_directory)
                return ModelCodeGraphRefreshResult(
                    outcome=EnumRefreshOutcome.DEFERRED,
                    built_at=built_at,
                    built_heads=built_heads,
                    attempted_at=read_attempt(self._state_directory),
                    reason="build-lock-held",
                )
            return self._decide()

    def _decide(self) -> ModelCodeGraphRefreshResult:
        built_at, built_heads = read_marker(self._state_directory)
        attempted_at = read_attempt(self._state_directory)
        try:
            current_heads = {
                repo.name: self._heads.head(repo.name, repo.ref)[:12]
                for repo in self._overlay.repos
            }
        except RepoSourceError as exc:
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.FAILED,
                built_at=built_at,
                built_heads=built_heads,
                attempted_at=attempted_at,
                changed=tuple(repo.name for repo in self._overlay.repos),
                reason=f"heads-unreadable:{reason_token(str(exc))}",
            )
        changed = tuple(
            repo.name
            for repo in self._overlay.repos
            if current_heads[repo.name] != built_heads.get(repo.name)
        )
        if not changed:
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.FRESH,
                built_at=built_at,
                built_heads=built_heads,
                attempted_at=attempted_at,
                current_heads=current_heads,
                reason="heads-unchanged",
            )
        now = self._clock().astimezone(UTC)
        recent = now - self._min_interval
        if built_at is not None and built_at > recent:
            reason = "built-too-recently"
        elif attempted_at is not None and attempted_at > recent:
            reason = "attempted-too-recently"
        else:
            reason = ""
        if reason:
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.DEFERRED,
                built_at=built_at,
                built_heads=built_heads,
                attempted_at=attempted_at,
                current_heads=current_heads,
                changed=changed,
                reason=reason,
            )
        # The attempt is recorded before the build, so a build that fails or dies leaves it behind.
        record_attempt(self._state_directory, now)
        result = self._run()
        if result.status != "completed":
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.FAILED,
                built_at=built_at,
                built_heads=built_heads,
                attempted_at=now,
                current_heads=current_heads,
                changed=changed,
                reason=reason_token(result.failure) if result.failure else "run-failed",
                result=result,
            )
        # The marker is what the build left, not what this decision assumed: read it back.
        marker_at, marker_heads = read_marker(self._state_directory)
        if marker_at is None or (marker_at, marker_heads) == (built_at, built_heads):
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.FAILED,
                built_at=built_at,
                built_heads=built_heads,
                attempted_at=now,
                current_heads=current_heads,
                changed=changed,
                reason="marker-not-rewritten",
                result=result,
            )
        return ModelCodeGraphRefreshResult(
            outcome=EnumRefreshOutcome.REFRESHED,
            built_at=marker_at,
            built_heads=marker_heads,
            attempted_at=now,
            current_heads=current_heads,
            changed=changed,
            reason="heads-moved",
            result=result,
        )
