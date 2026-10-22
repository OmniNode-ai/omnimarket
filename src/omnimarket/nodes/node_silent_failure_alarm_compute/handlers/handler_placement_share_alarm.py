# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether lab-fill is dispatching lanes the runner does not place (OMN-17427).

THE INCIDENT. On 2026-10-09 lab-fill dispatched 147 lanes between 00:10Z and 05:30Z and the
runner placed 5, all in the first run: every later lane ended ``repo-unresolved``. Each run wrote
its STATUS row with ``dispatched`` and ``placed`` and each lane wrote its terminal receipt, and
nobody read either. The lab idle alarm did not fire because lanes were dispatched.

WHAT IS DECIDED. Placed under half of dispatched over the newest two runs that dispatched
anything, or one refusal reason on most of the newest run's lanes, is an alarm.
A run that dispatched nothing is not evidence either way. The alarm names the refusal
reason the lane receipts report; when none reports one it says so rather than guessing.
"""

from __future__ import annotations

from collections import Counter
from typing import Literal

from ..models import (
    ModelLaneRefusal,
    ModelPlacementRun,
    ModelPlacementShareRequest,
    ModelPlacementShareResult,
)

LANE = "silent-failure-alarm"
TICKET = "OMN-17427"
FILL_INTERVAL_MINUTES = 20
NO_REASON = "no-refusal-reported"

__all__ = ["HandlerPlacementShareAlarm", "ModelPlacementShareResult"]


def _cell(value: str) -> str:
    """Free text must never create extra ledger cells."""
    return " ".join(value.replace("|", "/").split())


def _majority(
    run: ModelPlacementRun,
    refusals: tuple[ModelLaneRefusal, ...],
    share: float,
    least: int,
) -> bool:
    counts = Counter(r.status for r in refusals if r.run_key == run.run_key)
    if not counts:
        return False
    _, top = counts.most_common(1)[0]
    lanes = max(run.dispatched, sum(counts.values()))
    return top >= least and top > lanes * share


class HandlerPlacementShareAlarm:
    """Placement share and refusal-reason alarm over the newest acting lab-fill runs."""

    def handle(self, request: ModelPlacementShareRequest) -> ModelPlacementShareResult:
        """Return the verdict, and the FRICTION cells and alert text when it alarms."""
        acting = sorted(
            (r for r in request.runs if r.stamp <= request.now and r.dispatched > 0),
            key=lambda r: r.stamp,
        )
        window = tuple(acting[-request.consecutive_runs :])
        kinds: list[Literal["placement-share", "refusal-reason"]] = []
        pair = len(window) == request.consecutive_runs and all(
            r.placed < r.dispatched * request.share_floor for r in window
        )
        if pair:
            kinds.append("placement-share")
        # Only the newest run: an older run's refusals are history once a later run places.
        if window and _majority(
            window[-1],
            request.refusals,
            request.refusal_share,
            request.refusal_min_lanes,
        ):
            kinds.append("refusal-reason")
        if not kinds:
            return ModelPlacementShareResult(alarm=False)

        keys = tuple(r.run_key for r in window)
        reasons = Counter(r.status for r in request.refusals if r.run_key in keys)
        reason = (
            sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[0][0]
            if reasons
            else NO_REASON
        )
        dispatched = sum(r.dispatched for r in window)
        placed = sum(r.placed for r in window)
        lost = max(dispatched - placed, sum(reasons.values()))
        runs = ",".join(keys)
        detail = (
            f"lab-fill placed {placed} of {dispatched} dispatched lanes over runs {runs}; "
            f"refusal reason {reason}"
        )
        cells = (
            f"FRICTION | lane={LANE} | ticket={TICKET} | actor=launchd:{LANE} | "
            f"class={'-'.join(kinds)} | reason={_cell(reason)} | runs={runs} | "
            f"dispatched={dispatched} | placed={placed} | "
            f"cost=est ~{lost * FILL_INTERVAL_MINUTES} lane-minutes "
            f"(unplaced lanes x the {FILL_INTERVAL_MINUTES}m fill interval) | {detail}"
        )
        return ModelPlacementShareResult(
            alarm=True,
            kinds=tuple(kinds),
            reason=reason,
            runs=keys,
            dispatched=dispatched,
            placed=placed,
            friction_cells=cells,
            alert_text=f"LAB PLACEMENT {detail}",
        )
