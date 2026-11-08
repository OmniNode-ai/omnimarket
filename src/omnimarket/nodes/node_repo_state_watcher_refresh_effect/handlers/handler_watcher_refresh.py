# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerWatcherRefresh: run the host's declared refresh steps, then read the state again (OMN-23033).

Canonical handler: ``handle(request: ModelWatcherRefreshRequest) -> ModelWatcherRefreshResult``.

The steps stop at the first failure. After the steps the handler reads the watcher state's
read time: it answers ``refreshed`` only when that time moved forward, ``unchanged`` when the
steps succeeded and the state did not move (the watcher host did not tick, or nothing carried
the tick to this host), and ``failed`` when a step failed. Only ``refreshed`` is ok.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.nodes.node_repo_state_watcher_refresh_effect.models.model_watcher_refresh import (
    EnumWatcherRefreshStatus,
    ModelWatcherRefreshRequest,
    ModelWatcherRefreshResult,
    ModelWatcherRefreshStep,
)
from omnimarket.nodes.node_repo_state_watcher_refresh_effect.protocols.protocol_watcher_refresh import (
    ProtocolRefreshStepRunner,
    ProtocolWatcherStateClock,
)

REPO_PLACEHOLDER = "{repo}"


class HandlerWatcherRefresh:
    def __init__(
        self,
        *,
        clock: ProtocolWatcherStateClock,
        steps: Sequence[tuple[str, Sequence[str]]],
        runner: ProtocolRefreshStepRunner,
    ) -> None:
        if not steps:
            raise ValueError("the refresh declares no step")
        self._clock = clock
        self._steps = [(name, list(argv)) for name, argv in steps]
        self._runner = runner

    def handle(self, request: ModelWatcherRefreshRequest) -> ModelWatcherRefreshResult:
        before = self._clock.read_at()
        done: list[ModelWatcherRefreshStep] = []
        for name, argv in self._steps:
            step = self._runner.run(
                name, [part.replace(REPO_PLACEHOLDER, request.repo) for part in argv]
            )
            done.append(step)
            if not step.ok:
                return ModelWatcherRefreshResult(
                    repo=request.repo,
                    status=EnumWatcherRefreshStatus.FAILED,
                    ok=False,
                    read_at_before=before,
                    read_at_after=self._clock.read_at(),
                    steps=tuple(done),
                    message=f"step {name} failed",
                )
        after = self._clock.read_at()
        moved = after is not None and (before is None or after > before)
        return ModelWatcherRefreshResult(
            repo=request.repo,
            status=(
                EnumWatcherRefreshStatus.REFRESHED
                if moved
                else EnumWatcherRefreshStatus.UNCHANGED
            ),
            ok=moved,
            read_at_before=before,
            read_at_after=after,
            steps=tuple(done),
            message=(
                ""
                if moved
                else "the steps succeeded and the watcher state's read time did not move"
            ),
        )


__all__ = ["REPO_PLACEHOLDER", "HandlerWatcherRefresh"]
