# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""VS-17: the caller that feeds a finalized window's seam readings to the throughput tick.

Pure. `handle` turns the rows of the `view_seam_readings` exposure into the typed field the tick
consumes; `vs17_counts` and `vs17_closes` build the seam's own count and window-close events from
the tick's verdict. Both sides of VS-17 are emitted here (the seam is marked one emitter, the weaker
reading). A reading of another window is not this window's reading, and a second reading of one seam
takes the later `read_at`.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.models.seam_reading import (
    EnumSeamCrossingState,
    EnumSeamDisposition,
    EnumSeamMeasureKind,
    EnumSeamSide,
    EnumSeamUnit,
    ModelSeamCount,
    ModelSeamIdDisposition,
    ModelSeamMeasure,
    ModelSeamReading,
    ModelSeamReadingsFacts,
    ModelSeamWindowClose,
)
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_readings_feed import (
    ModelSeamReadingsFeedRequest,
    ModelSeamReadingsFeedResult,
)

VS17 = "VS-17"


def _window_readings(request: ModelSeamReadingsFeedRequest) -> list[ModelSeamReading]:
    latest: dict[str, ModelSeamReading] = {}
    for reading in request.recorded:
        if reading.window != request.window:
            continue
        held = latest.get(reading.seam_id)
        if held is None or reading.read_at >= held.read_at:
            latest[reading.seam_id] = reading
    return list(latest.values())


def _crossing_id(request: ModelSeamReadingsFeedRequest) -> str:
    return f"{request.tick_id}:{VS17}"


class HandlerSeamReadingsFeed:
    def handle(
        self, request: ModelSeamReadingsFeedRequest
    ) -> ModelSeamReadingsFeedResult:
        return ModelSeamReadingsFeedResult(
            facts=ModelSeamReadingsFacts(
                window=request.window,
                finalized=request.finalized,
                publish_steps_ok=request.publish_steps_ok,
                expected_seam_ids=request.expected_seam_ids,
                readings=tuple(_window_readings(request)),
                declared_seam_ids=request.declared_seam_ids,
            )
        )

    def vs17_counts(
        self,
        request: ModelSeamReadingsFeedRequest,
        *,
        consumed_ids: Sequence[str],
        missing_ids: Sequence[str],
        unknown_ids: Sequence[str],
    ) -> tuple[ModelSeamCount, ...]:
        """Sender: the recorded readings and the findings they owe. Receiver: what the tick consumed and raised."""
        readings = _window_readings(request)
        recorded_ids = [r.seam_id for r in readings]
        owed = [
            r.seam_id for r in readings if r.state is not EnumSeamCrossingState.MATCHED
        ]
        owed += [s for s in request.expected_seam_ids if s not in recorded_ids]
        raised = [*missing_ids, *unknown_ids]

        def ids(
            values: Sequence[str], disposition: EnumSeamDisposition, prefix: str = ""
        ) -> tuple[ModelSeamIdDisposition, ...]:
            return tuple(
                ModelSeamIdDisposition(
                    id=f"{prefix}{value}", disposition=disposition, stamp=request.now
                )
                for value in values
            )

        def count(
            side: EnumSeamSide,
            disposition: EnumSeamDisposition,
            readings_ids: Sequence[str],
            finding_ids: Sequence[str],
        ) -> ModelSeamCount:
            return ModelSeamCount(
                seam_id=VS17,
                side=side,
                crossing_id=_crossing_id(request),
                origin_tick_id=request.tick_id,
                measures=(
                    ModelSeamMeasure(
                        measure_id="readings",
                        unit=EnumSeamUnit.READINGS,
                        kind=EnumSeamMeasureKind.TRANSIT,
                        ids=ids(readings_ids, disposition),
                    ),
                    ModelSeamMeasure(
                        measure_id="findings",
                        unit=EnumSeamUnit.FINDINGS,
                        kind=EnumSeamMeasureKind.TRANSIT,
                        ids=ids(finding_ids, disposition, "seam:"),
                    ),
                ),
            )

        return (
            count(
                EnumSeamSide.SENDER, EnumSeamDisposition.HANDED_OVER, recorded_ids, owed
            ),
            count(
                EnumSeamSide.RECEIVER,
                EnumSeamDisposition.ACCEPTED,
                consumed_ids,
                raised,
            ),
        )

    def vs17_closes(
        self, request: ModelSeamReadingsFeedRequest
    ) -> tuple[ModelSeamWindowClose, ...]:
        return tuple(
            ModelSeamWindowClose(
                seam_id=VS17,
                side=side,
                window=request.window,
                reported_through=request.now,
                crossing_ids=(_crossing_id(request),),
            )
            for side in (EnumSeamSide.SENDER, EnumSeamSide.RECEIVER)
        )
