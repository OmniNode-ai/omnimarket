# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule verdict on the runtime's tick (OMN-19998).

handle takes the runtime tick. A tick inside the overlay's interval returns nothing and does not move the last-run
time; the tick that is due loads the declared job overlays, builds the request from the run store's held states and
returns the verdict for the runtime to publish. A declared schedule that has not fired inside its interval plus grace
reads red.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import UTC, datetime

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.schedule_gate import ScheduleGate
from omnimarket.handlers.schedule_run_store import get_schedule_run_store
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleBlock,
    ModelScheduleUnreadableJob,
    ModelScheduleVerdictOverlay,
    ModelScheduleVerdictRequest,
    ModelScheduleVerdictResult,
)
from omnimarket.nodes.node_schedule_verdict_compute.handlers.handler_schedule_verdict import (
    HandlerScheduleVerdict,
)
from omnimarket.protocols.protocol_schedule_run_store import ProtocolScheduleRunStore

SCHEDULE_OVERLAY_ID = "node_schedule_verdict_effect"


class HandlerScheduleVerdictScheduled:
    """Gate on the schedule, load the declared overlays, compute the verdict, return it."""

    def __init__(
        self,
        *,
        gate: ScheduleGate | None = None,
        store: ProtocolScheduleRunStore | None = None,
        compute: HandlerScheduleVerdict | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._gate = gate or ScheduleGate(
            SCHEDULE_OVERLAY_ID,
            model=ModelScheduleVerdictOverlay,
            environ=environ,
        )
        self._store = store
        self._compute = compute or HandlerScheduleVerdict()
        self._environ = environ
        self._observed_since: datetime | None = None
        self._refusals = ScheduledOverlayRefusalLatch(SCHEDULE_OVERLAY_ID)

    def _env(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def handle(self, request: ModelRuntimeTick) -> ModelScheduleVerdictResult | None:
        try:
            overlay = self._gate.declaration()
            if not isinstance(overlay, ModelScheduleVerdictOverlay):
                raise NodeOverlayNotFoundError(
                    f"{SCHEDULE_OVERLAY_ID}: the overlay is not a verdict overlay"
                )
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            return None
        now = request.now.astimezone(UTC)
        if self._observed_since is None:
            self._observed_since = now
        if not self._gate.due(now):
            return None
        self._gate.mark(now)
        declarations: list[ModelScheduleBlock] = []
        unreadable: list[ModelScheduleUnreadableJob] = []
        for job_overlay_id in overlay.job_overlay_ids:
            try:
                declarations.append(
                    load_structured_overlay(
                        job_overlay_id,
                        ModelScheduleBlock,
                        self._env(),
                    )
                )
            except NodeOverlayError as exc:
                unreadable.append(
                    ModelScheduleUnreadableJob(
                        overlay_id=job_overlay_id, detail=str(exc)
                    )
                )
        store = self._store or get_schedule_run_store()
        result = self._compute.handle(
            ModelScheduleVerdictRequest(
                now=now,
                observed_since=self._observed_since,
                declared=tuple(declarations),
                states=store.state(),
                unreadable=tuple(unreadable),
            )
        )
        self._refusals.recovered()
        return result
