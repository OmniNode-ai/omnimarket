# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The stopped-line fill's tick on the runtime's schedule (OMN-24029).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due builds the fill handler on the profile the overlay names, runs one fill tick and returns
the run event. A refused overlay, or a live profile whose store DSN variable is unset, is a failed run event that names it, never a silent skip.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
    resolve_setting,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleRunEvent,
)
from omnimarket.nodes.node_floor_fill_orchestrator.container.container_inmemory import (
    build_inmemory_handler,
)
from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
    build_live_handler_factory,
)
from omnimarket.nodes.node_floor_fill_orchestrator.handlers.handler_floor_fill_command import (
    HandlerFloorFillCommand,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    ModelFloorFillCommandRequest,
    ModelFloorFillConfig,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_overlay_document import (
    ModelFloorFillOverlayDocument,
)
from omnimarket.nodes.node_floor_fill_scheduled_effect.models import (
    ModelFloorFillScheduleOverlay,
)

NODE_NAME = "node_floor_fill_scheduled_effect"


class HandlerFloorFillScheduled:
    """Gate on the schedule, build the fill on its profile, run one tick, return the run event."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        build_handler: Callable[[ModelFloorFillConfig], HandlerFloorFillCommand]
        | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._gate = gate or ScheduleGate(
            NODE_NAME, model=ModelFloorFillScheduleOverlay, environ=environ
        )
        self._build_handler = build_handler
        self._environ = environ
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
            if not isinstance(overlay, ModelFloorFillScheduleOverlay):
                raise NodeOverlayNotFoundError(
                    f"{NODE_NAME}: the overlay is not a fill schedule overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        try:
            document = load_structured_overlay(
                overlay.fill_overlay_id, ModelFloorFillOverlayDocument, self._environ
            )
        except NodeOverlayError:
            return self._event(
                request,
                "failed",
                f"the fill overlay {overlay.fill_overlay_id} could not be read",
            )
        config = document.floor_fill
        build = self._build_handler
        if build is None:
            if overlay.profile == "live":
                live_settings = document.floor_fill_live
                try:
                    dsn = resolve_setting(
                        NODE_NAME,
                        "store_dsn",
                        fields=frozenset(),
                        env_vars=(live_settings.store_dsn_env,),
                        environ=self._environ,
                    )
                except NodeOverlayError:
                    return self._event(
                        request,
                        "failed",
                        f"UNKNOWN fill-state:store-dsn-unset:{live_settings.store_dsn_env}",
                    )
                build = build_live_handler_factory(live_settings, dsn=dsn)
            else:
                build = build_inmemory_handler
        result = build(config).handle(
            ModelFloorFillCommandRequest(
                command=EnumFloorFillCommand.TICK, key=None, host_id=config.host_id
            )
        )
        first_line = result.stdout.splitlines()[0] if result.stdout else ""
        return self._event(
            request, "completed" if result.exit_code == 0 else "failed", first_line
        )


__all__: list[str] = ["HandlerFloorFillScheduled"]
