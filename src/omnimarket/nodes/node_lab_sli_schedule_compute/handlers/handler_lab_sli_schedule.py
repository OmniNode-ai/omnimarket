# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab SLI schedule handler: one collect request per due interval, gated on the pack."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, timedelta

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.lab_sli_pack_gate import lab_sli_pack_enabled
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_runtime_tick_schedule import scheduled_fire
from omnimarket.nodes.node_lab_sli_collect_effect.models.model_lab_sli_collect import (
    ModelLabSliCollectRequest,
)
from omnimarket.nodes.node_lab_sli_schedule_compute.models.model_lab_sli_schedule_overlay import (
    ModelLabSliScheduleOverlay,
)

NODE_NAME = "node_lab_sli_schedule_compute"


class HandlerLabSliSchedule:
    """Turn the platform runtime tick into the lab SLI collect request, once per interval."""

    def __init__(
        self,
        overlay: ModelLabSliScheduleOverlay | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._environ = environ
        self._latch = ScheduledOverlayRefusalLatch(NODE_NAME)

    def handle(self, request: ModelRuntimeTick) -> ModelLabSliCollectRequest | None:
        """The collect request for the due window, or None when the pack is off, the overlay is missing or the tick opens no window."""
        if not lab_sli_pack_enabled(self._environ):
            return None
        overlay = self._overlay
        if overlay is None:
            try:
                overlay = load_structured_overlay(
                    NODE_NAME, ModelLabSliScheduleOverlay, self._environ
                )
            except NodeOverlayNotFoundError as exc:
                self._latch.refuse(exc)
                return None
            self._overlay = overlay
        fire = scheduled_fire(
            overlay.schedule,
            now=request.now,
            scheduled_at=request.scheduled_at,
            tick_interval_ms=request.tick_interval_ms,
            tick_id=request.tick_id,
            scheduler_id=request.scheduler_id,
        )
        if fire is None:
            return None
        return ModelLabSliCollectRequest(
            interval_start=fire.window_start - timedelta(seconds=fire.interval_seconds),
            now=request.now.astimezone(UTC),
            collector_host=overlay.collector_host,
            pr_watcher_state_path=overlay.pr_watcher_state_path,
            disk_paths=overlay.disk_paths,
        )


__all__ = ["HandlerLabSliSchedule"]
