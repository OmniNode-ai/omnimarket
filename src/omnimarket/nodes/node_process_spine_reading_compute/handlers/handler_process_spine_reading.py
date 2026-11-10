# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Piece 7's reading: the hourly drill and the replay, judged at a read time.

``handle(ModelProcessSpineReadingRequest) -> ModelProcessSpineReading``. No clock read, no I/O.
UNKNOWN or stale counts as NOT WORKING for every reader that applies the plan's rule; this node reports which.

* the drill did not run: UNKNOWN, ``drill-unread``; the drill's send or control send errored: UNKNOWN;
* the drill is older than the max age: NOT_WORKING, ``stale-drill`` (a probe dated after the read time is
  ``future-dated-drill``);
* the stop did not commit: NOT_WORKING, ``stop-not-committed``;
* the drill's lane was admitted above the stop: NOT_WORKING, ``instance-accepted-drill-lane`` (the fault the plan
  injects on purpose);
* the control lane was refused: NOT_WORKING, ``control-lane-refused`` (an instance that refuses everything proves
  nothing about the stop);
* the replay is required and absent: UNKNOWN, ``replay-unread``; present with a mismatch: NOT_WORKING,
  ``replay-mismatch``.
"""

from __future__ import annotations

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_process_spine_reading_compute.models import (
    EnumDrillSend,
    ModelProcessSpineReading,
    ModelProcessSpineReadingRequest,
)


class HandlerProcessSpineReading:
    """Compute piece 7's reading for one read time."""

    def handle(
        self, request: ModelProcessSpineReadingRequest
    ) -> ModelProcessSpineReading:
        drill = request.drill
        failing: list[str] = []
        unknown: list[str] = []
        if drill is None:
            unknown.append("drill-unread")
        else:
            age = int((request.now - drill.probed_at).total_seconds())
            if age < 0:
                failing.append("future-dated-drill")
            elif age > request.max_age_seconds:
                failing.append("stale-drill")
            if EnumDrillSend.ERRORED in (drill.drill_send, drill.control_send):
                unknown.append("drill-send-errored")
            if not drill.stop_committed:
                failing.append("stop-not-committed")
            if drill.drill_send is EnumDrillSend.ADMITTED:
                failing.append("instance-accepted-drill-lane")
            if drill.control_send is EnumDrillSend.REFUSED:
                failing.append("control-lane-refused")
        replay = request.replay
        if replay is None:
            if request.replay_required:
                unknown.append("replay-unread")
        elif replay.mismatches > 0:
            failing.append("replay-mismatch")
        if failing:
            verdict, reasons = EnumReadingVerdict.NOT_WORKING, failing
        elif unknown:
            verdict, reasons = EnumReadingVerdict.UNKNOWN, unknown
        else:
            verdict, reasons = EnumReadingVerdict.WORKING, ["ok"]
        return ModelProcessSpineReading(
            verdict=verdict,
            reasons=tuple(reasons),
            probed_at=None if drill is None else drill.probed_at,
            published_at=request.now,
            max_age_seconds=request.max_age_seconds,
            drill_id=None if drill is None else drill.drill_id,
        )
