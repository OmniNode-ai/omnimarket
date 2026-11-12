# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The integration projection: the newest reading per change, the counts and the groups (OMN-23899).

Pure: no clock read, no state, no I/O. A change read twice in one batch keeps its newest reading by ``read_at`` (a tie
keeps the later one in the batch). The counts are over the kept rows; the groups follow the declaration order of
``EnumIntegrationGapClass`` and list each group's changes oldest merge first, ties by key.
"""

from __future__ import annotations

from omnimarket.models.integration_reading import (
    SETTLED_STATES,
    UNINTEGRATED_STATES,
    EnumIntegrationGapClass,
    EnumIntegrationState,
    ModelIntegrationCounts,
    ModelIntegrationGroup,
    ModelIntegrationOldest,
    ModelIntegrationProjection,
    ModelIntegrationReading,
    ModelIntegrationReadingBatch,
)


def _merge_order(reading: ModelIntegrationReading) -> tuple[str, str]:
    return reading.merged_at.isoformat(), reading.key


class HandlerIntegrationProjection:
    """``handle(batch) -> projection``."""

    def handle(
        self, request: ModelIntegrationReadingBatch
    ) -> ModelIntegrationProjection:
        newest: dict[str, ModelIntegrationReading] = {}
        for reading in request.readings:
            held = newest.get(reading.key)
            if held is None or reading.read_at >= held.read_at:
                newest[reading.key] = reading
        rows = dict(sorted(newest.items(), key=lambda item: _merge_order(item[1])))
        open_rows = [r for r in rows.values() if r.state in UNINTEGRATED_STATES]
        by_class = {
            gap_class: sum(1 for r in open_rows if r.gap_class is gap_class)
            for gap_class in EnumIntegrationGapClass
            if gap_class is not EnumIntegrationGapClass.NONE
        }
        oldest = min(open_rows, key=_merge_order, default=None)
        counts = ModelIntegrationCounts(
            merged=len(rows),
            seen_running=sum(
                1 for r in rows.values() if r.state is EnumIntegrationState.RUNNING
            ),
            settled_elsewhere=sum(
                1
                for r in rows.values()
                if r.state in SETTLED_STATES
                and r.state is not EnumIntegrationState.RUNNING
            ),
            reading_owed=sum(
                1 for r in rows.values() if r.state is EnumIntegrationState.UNPROVEN
            ),
            unintegrated=len(open_rows),
            by_gap_class=by_class,
            oldest_unintegrated=(
                ModelIntegrationOldest(key=oldest.key, merged_at=oldest.merged_at)
                if oldest is not None
                else None
            ),
        )
        groups = tuple(
            ModelIntegrationGroup(
                gap_class=gap_class,
                keys=tuple(
                    r.key
                    for r in sorted(open_rows, key=_merge_order)
                    if r.gap_class is gap_class
                ),
            )
            for gap_class, count in by_class.items()
            if count
        )
        return ModelIntegrationProjection(
            as_of=request.as_of,
            origin=request.origin,
            rows=rows,
            counts=counts,
            groups=groups,
        )


__all__ = ["HandlerIntegrationProjection"]
