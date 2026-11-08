# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold the seam family's events into one record, and the readings into the view_seam_readings rows.

A pure fold with one writer per record: a count replaces the row of its (seam, side, crossing) key,
so a replay never adds to it; a close replaces the close of its (seam, side, window); a reading
replaces the reading of its (seam, window) when its `read_at` is not older. The reducer holds no
lease, emits no terminal event of its own and has no clock.
"""

from __future__ import annotations

from omnimarket.models.seam_reading import ModelSeamReading, ModelSeamWindowClose
from omnimarket.nodes.node_view_seam_reading_reducer.models.model_view_seam_state import (
    ModelViewSeamCloseEvent,
    ModelViewSeamCountEvent,
    ModelViewSeamFoldRequest,
    ModelViewSeamFoldResult,
    ModelViewSeamReadingRow,
    ModelViewSeamState,
)


def reading_key(reading: ModelSeamReading) -> str:
    return f"{reading.seam_id}|{reading.window.start.isoformat()}|{reading.window.end.isoformat()}"


def close_key(close: ModelSeamWindowClose) -> str:
    return (
        f"{close.seam_id}|{close.side.value}|"
        f"{close.window.start.isoformat()}|{close.window.end.isoformat()}"
    )


def row_of(reading: ModelSeamReading) -> ModelViewSeamReadingRow:
    return ModelViewSeamReadingRow(
        seam_id=reading.seam_id,
        window_start=reading.window.start,
        window_end=reading.window.end,
        state=reading.state,
        crossings=reading.crossings,
        mismatched=reading.mismatched,
        unreadable=reading.unreadable,
        examples=reading.examples,
        read_at=reading.read_at,
    )


class HandlerViewSeamReadingReducer:
    def handle(self, request: ModelViewSeamFoldRequest) -> ModelViewSeamFoldResult:
        state, event = request.state, request.event
        if isinstance(event, ModelViewSeamCountEvent):
            key = "|".join(str(part) for part in event.count.key)
            return ModelViewSeamFoldResult(
                state=ModelViewSeamState(
                    counts={**state.counts, key: event.count},
                    closes=state.closes,
                    readings=state.readings,
                )
            )
        if isinstance(event, ModelViewSeamCloseEvent):
            return ModelViewSeamFoldResult(
                state=ModelViewSeamState(
                    counts=state.counts,
                    closes={**state.closes, close_key(event.close): event.close},
                    readings=state.readings,
                )
            )
        reading = event.reading
        key = reading_key(reading)
        held = state.readings.get(key)
        if held is not None and reading.read_at < held.read_at:
            return ModelViewSeamFoldResult(state=state, row=row_of(held))
        return ModelViewSeamFoldResult(
            state=ModelViewSeamState(
                counts=state.counts,
                closes=state.closes,
                readings={**state.readings, key: reading},
            ),
            row=row_of(reading),
        )
