# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerEvalTrackingSchedule: the runtime tick requests the weekly coding test (OMN-24177).

Plan task T13. Each tick names at most one run: the current window's run of the arm whose turn
the tick is (``arm_for_slot``), as a ``ModelCodeEvalRunRequest`` whose correlation id is the
run id. The decision reads only the tick and the overlay, keeps no state and reads no clock,
so a restart cannot repeat a window or skip one: the next turn of the arm names the same run,
and the trial driver drops a request for a run id it already holds. A tick before the record's
epoch returns nothing. The overlay is read on the first tick, so a runtime that loads this
package without it still boots.
"""

from __future__ import annotations

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.code_eval_trial.model_code_eval_run_request import (
    ModelCodeEvalArm,
    ModelCodeEvalRunRequest,
)
from omnimarket.models.eval_tracking.eval_tracking_window import (
    arm_for_slot,
    cadence_days,
    tracking_run_id,
    window_index,
)
from omnimarket.models.eval_tracking.model_eval_tracking_overlay import (
    ModelEvalTrackingOverlay,
)

OVERLAY_ID = "node_eval_tracking_schedule_compute"


def tracking_overlay() -> ModelEvalTrackingOverlay:
    """The node's overlay; refuses when none supplies it."""
    return load_structured_overlay(OVERLAY_ID, ModelEvalTrackingOverlay)


class HandlerEvalTrackingSchedule:
    """COMPUTE handler: tick in, the current window's run request of one arm out."""

    def __init__(self, overlay: ModelEvalTrackingOverlay | None = None) -> None:
        self._overlay = overlay
        self._record_sha: str | None = None

    def handle(self, request: ModelRuntimeTick) -> ModelCodeEvalRunRequest | None:
        if self._overlay is None:
            self._overlay = tracking_overlay()
        overlay = self._overlay
        record = overlay.record
        if self._record_sha is None:
            self._record_sha = record.sha256()
        slot = int(request.scheduled_at.timestamp() * 1000) // request.tick_interval_ms
        resend_ticks = max(1, overlay.resend_seconds * 1000 // request.tick_interval_ms)
        turn = arm_for_slot(slot, len(record.arms), resend_ticks)
        if turn is None:
            return None
        arm = record.arms[turn]
        days = cadence_days(overlay, arm)
        window = window_index(request.now, record.epoch, days)
        if window is None:
            return None
        return ModelCodeEvalRunRequest(
            correlation_id=tracking_run_id(self._record_sha, arm.model, days, window),
            tenant_id=record.tenant_id,
            harness_revision=record.harness_revision,
            publish_cells=False,
            candidates=record.candidates,
            arms=(ModelCodeEvalArm(model=arm.model, backend_id=arm.backend_id),),
        )


__all__: list[str] = ["HandlerEvalTrackingSchedule", "tracking_overlay"]
