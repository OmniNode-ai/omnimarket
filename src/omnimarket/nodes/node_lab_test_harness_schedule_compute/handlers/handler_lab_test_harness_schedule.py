# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerLabTestHarnessSchedule: the runtime tick decides when the central test harness runs.

The interval, offset and workflow name are the ``schedule`` key of the harness overlay
(``node_lab_test_harness_effect``), found under ``ONEX_SKILL_OVERLAY_ROOTS``. The contract
carries none of them and there is no packaged default: an absent or invalid overlay refuses.
The decision reads only the tick, so the handler holds no state.
"""

from __future__ import annotations

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessOverlay,
    ModelLabTestHarnessRequested,
)
from omnimarket.models.model_runtime_tick_schedule import (
    ModelRuntimeTickScheduleConfig,
    scheduled_fire,
)

OVERLAY_ID = "node_lab_test_harness_effect"


def schedule_config() -> ModelRuntimeTickScheduleConfig:
    """The schedule block of the harness overlay; refuses when no overlay supplies it."""
    return load_structured_overlay(OVERLAY_ID, ModelLabTestHarnessOverlay).schedule


class HandlerLabTestHarnessSchedule:
    """COMPUTE handler: tick in, one harness run request out per interval."""

    def __init__(self, config: ModelRuntimeTickScheduleConfig | None = None) -> None:
        self._cfg = config or schedule_config()

    def handle(self, request: ModelRuntimeTick) -> ModelLabTestHarnessRequested | None:
        fire = scheduled_fire(
            self._cfg,
            now=request.now,
            scheduled_at=request.scheduled_at,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )
        if fire is None:
            return None
        return ModelLabTestHarnessRequested(
            run_id=fire.fire_id, requested_at=fire.tick_now, trigger=fire.workflow
        )


__all__: list[str] = ["HandlerLabTestHarnessSchedule", "schedule_config"]
