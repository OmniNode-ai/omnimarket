# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refresh on merge: rebuild the graph only when a repository head moved.

The nightly build runs once a day, so a merge can wait that long to reach the graph. The refresh
reads the heads of the last build from the state directory's ``code-graph-built.json``, reads each
declared repository's head now, and runs the writing build only when some head moved and the last
build is at least :data:`REFRESH_MIN_INTERVAL` old, so a burst of merges builds once instead of
once per tick. Unchanged heads are ``fresh`` and append nothing; a too-young build is ``deferred``;
a head that cannot be read, or a build that fails, is ``failed`` - the refresh is never fresh when
it cannot prove the heads unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.model_code_graph import (
    EnumRefreshOutcome,
    ModelCodeGraphRefreshResult,
    ModelCodeGraphResult,
)
from omnimarket.nodes.node_code_graph_effect.overlay import ModelCodeGraphOverlay
from omnimarket.nodes.node_code_graph_effect.protocols import (
    ProtocolRepoHeads,
    RepoSourceError,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    reason_token,
)

#: A build younger than this defers the refresh, so a burst of merges builds once.
REFRESH_MIN_INTERVAL = timedelta(minutes=15)
#: What the writing build leaves in the state directory; the refresh reads it instead of rebuilding.
BUILT_MARKER = "code-graph-built.json"


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

    def _built(self) -> tuple[datetime | None, dict[str, str]]:
        """The last build's time and heads from the marker; none when it is absent or unreadable."""
        try:
            document = json.loads(
                (self._state_directory / BUILT_MARKER).read_text(encoding="utf-8")
            )
            built_at = datetime.fromisoformat(str(document["built_at"]))
            heads = {str(name): str(value) for name, value in document["heads"].items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None, {}
        return built_at.astimezone(UTC), heads

    def handle(self) -> ModelCodeGraphRefreshResult:
        """Fresh when no head moved, deferred while the last build is young, else build."""
        built_at, built_heads = self._built()
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
                current_heads=current_heads,
                reason="heads-unchanged",
            )
        now = self._clock().astimezone(UTC)
        if built_at is not None and built_at > now - self._min_interval:
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.DEFERRED,
                built_at=built_at,
                built_heads=built_heads,
                current_heads=current_heads,
                changed=changed,
                reason="built-too-recently",
            )
        result = self._run()
        if result.status != "completed":
            return ModelCodeGraphRefreshResult(
                outcome=EnumRefreshOutcome.FAILED,
                built_at=built_at,
                built_heads=built_heads,
                current_heads=current_heads,
                changed=changed,
                reason=result.failure or "run-failed",
                result=result,
            )
        return ModelCodeGraphRefreshResult(
            outcome=EnumRefreshOutcome.REFRESHED,
            built_at=now,
            built_heads=dict(current_heads),
            current_heads=current_heads,
            changed=changed,
            reason="heads-moved",
            result=result,
        )
