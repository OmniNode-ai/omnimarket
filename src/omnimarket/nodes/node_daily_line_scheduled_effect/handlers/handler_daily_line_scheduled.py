# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run one daily line when the runtime tick reaches its schedule (OMN-25163).

A missing overlay refuses once. A due tick builds and runs the daily line runner,
returning a typed event that reports the publication or names the failure.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_daily_line import ModelDailyLinePublication
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_daily_line_compute import HandlerDailyLine
from omnimarket.nodes.node_daily_line_effect import (
    HandlerDailyLineRun,
)
from omnimarket.nodes.node_daily_line_effect import (
    build_runner as build_daily_line_runner,
)
from omnimarket.nodes.node_daily_line_scheduled_effect.models import (
    ModelDailyLineScheduleOverlay,
)

NODE_NAME = "node_daily_line_scheduled_effect"
#: The most characters one run event's detail carries.
DETAIL_LIMIT = 300


class HandlerDailyLineScheduled:
    """Gate on the schedule, build the runner, publish one daily line and report."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        build_runner: Callable[[], HandlerDailyLineRun] | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelDailyLineScheduleOverlay, environ=environ
        )
        self._build_runner = build_runner or (
            lambda: build_daily_line_runner(HandlerDailyLine(), environ)
        )
        self._clock = clock
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def _event(
        self,
        request: ModelRuntimeTick,
        outcome: Literal["completed", "failed"],
        detail: str,
    ) -> ModelScheduleRunEvent:
        return self._gate.run_event(
            tick_id=str(request.tick_id),
            scheduled_for=request.now,
            fired_at=self._clock(),
            outcome=outcome,
            detail=detail,
        )

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleRunEvent | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelDailyLineScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a daily line schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            publication = self._build_runner().run()
            detail = self._detail(publication)
        except Exception as exc:
            return self._event(
                request, "failed", f"{type(exc).__name__}: {exc}"[:DETAIL_LIMIT]
            )
        return self._event(request, "completed", detail)

    @staticmethod
    def _detail(publication: ModelDailyLinePublication) -> str:
        """The published line's first line, then the unknown parts, in at most DETAIL_LIMIT characters."""
        lines = publication.line.splitlines()
        first = lines[0] if lines else ""
        if not first.strip():
            raise ValueError("the published daily line is empty")
        suffix = (
            f" (unknown: {','.join(publication.unknown_parts)})"
            if publication.unknown_parts
            else ""
        )
        return first[: max(DETAIL_LIMIT - len(suffix), 0)] + suffix


__all__: list[str] = ["HandlerDailyLineScheduled"]
