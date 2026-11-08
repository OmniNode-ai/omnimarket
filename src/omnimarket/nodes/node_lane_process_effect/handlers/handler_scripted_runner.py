# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A scripted ProtocolLaneProcessRunner: records each call in order and answers what the test scripted."""

from __future__ import annotations

from collections import deque
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelLaneProcessRunResult,
    ModelLaneProcessStartResult,
    ModelLaneStartSpec,
)


class HandlerScriptedRunner:
    """Answers the scripted results in order; a call with nothing scripted fails loud."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        *,
        starts: tuple[ModelLaneProcessStartResult, ...] = (),
        runs: tuple[ModelLaneProcessRunResult, ...] = (),
    ) -> None:
        self._starts = deque(starts)
        self._runs = deque(runs)
        self.calls: list[tuple[str, ModelLaneStartSpec, float | None]] = []

    def start_detached(self, spec: ModelLaneStartSpec) -> ModelLaneProcessStartResult:
        self.calls.append(("start_detached", spec, None))
        if not self._starts:
            raise LookupError("no start result scripted")
        return self._starts.popleft()

    def run_bounded(
        self, spec: ModelLaneStartSpec, timeout_seconds: float
    ) -> ModelLaneProcessRunResult:
        self.calls.append(("run_bounded", spec, timeout_seconds))
        if not self._runs:
            raise LookupError("no run result scripted")
        return self._runs.popleft()
