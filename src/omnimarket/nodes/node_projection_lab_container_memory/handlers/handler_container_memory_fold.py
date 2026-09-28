# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold over one lane container memory event (OMN-19961).

Rule 7a definition-B: ``handle(request) -> result``. No clock, no broker, no
database. Every value a row carries is a field of the event, so the same event
always folds to the same rows and a replay rewrites identical values.

This class is half of the pair. ``LabContainerMemoryProjectionWriter`` beside
it is the entry the runtime calls; a pure entry alone on a projection arm
validates, returns and stores nothing while every watermark reads healthy.
"""

from __future__ import annotations

from omnimarket.nodes.node_projection_lab_container_memory.models import (
    ModelContainerMemoryFoldResult,
    ModelContainerMemoryRow,
    ModelLaneContainerMemoryEvent,
)


class HandlerContainerMemoryFold:
    """Folds one memory event into one row per record."""

    def handle(
        self, request: ModelLaneContainerMemoryEvent
    ) -> ModelContainerMemoryFoldResult:
        rows = tuple(
            ModelContainerMemoryRow(
                record_key=record.record_key,
                host=request.host,
                host_boot_id=request.host_boot_id,
                lane=record.lane,
                container_id=record.container_id,
                container_name=record.container_name,
                container_started_at=record.container_started_at,
                limit_bytes=record.limit_bytes,
                peak_bytes=record.peak_bytes,
                peak_window_start=record.peak_window_start,
                peak_window_end=record.peak_window_end,
                window_start=request.window_start,
                window_end=request.window_end,
                max_total=record.max_total,
                max_delta=record.max_delta,
                oom_kill_total=record.oom_kill_total,
                oom_kill_delta=record.oom_kill_delta,
                ci_runs=request.ci_runs,
            )
            # Ordered by the key, so the result is byte-identical for one
            # event whatever order the producer listed its records in.
            for record in sorted(request.records, key=lambda r: r.record_key)
        )
        return ModelContainerMemoryFoldResult(rows=rows)


__all__ = ["HandlerContainerMemoryFold"]
