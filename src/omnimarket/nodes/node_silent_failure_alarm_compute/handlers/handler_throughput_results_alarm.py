# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether the last hour produced merges and placed lanes (OMN-19998).

THE INCIDENT. From 00:01Z to 03:05Z on 2026-10-16 the orchestrator session was logged out, yet the
tick-liveness alarm said LIVE: the launchd throughput tick refreshed the same heartbeat the alarm
read. A heartbeat proves a timer ran, not that anything landed.

WHAT IS DECIDED. Results, not marks: fewer merges in the last hour than the bound while non-draft
PRs are open, or fewer lanes placed on a pool host than the bound, is an alarm. A source the caller
could not read is UNKNOWN and is raised too, because an unread count is not a pass. A bound of
zero turns that measure off.
"""

from __future__ import annotations

from ..models.model_throughput_results_alarm import (
    ModelThroughputResultsRequest,
    ModelThroughputResultsResult,
)

LANE = "silent-failure-alarm"
TICKET = "OMN-19998"
FIX = (
    "read /omni:queue-status for the stopped repositories and the landing controller's "
    "escalations, and the throughput tick's own log for its floor escalation"
)

__all__ = ["HandlerThroughputResultsAlarm", "ModelThroughputResultsResult"]


class HandlerThroughputResultsAlarm:
    """Merges and lane placements in the last hour, against their bounds."""

    def handle(
        self, request: ModelThroughputResultsRequest
    ) -> ModelThroughputResultsResult:
        """Return PRODUCING, SHORT or UNKNOWN, with the alarm cells when not PRODUCING."""
        short: list[str] = []
        unknown = False
        if request.min_merges_per_hour:
            if request.merges_last_hour is None or request.open_prs is None:
                short.append("merges: the PR watcher state is unreadable")
                unknown = True
            elif (
                request.open_prs
                and request.merges_last_hour < request.min_merges_per_hour
            ):
                short.append(
                    f"{request.merges_last_hour} merges in the last hour with {request.open_prs} "
                    f"open non-draft PRs (bound {request.min_merges_per_hour})"
                )
        if request.min_lanes_placed_per_hour:
            if request.lanes_placed_last_hour is None:
                short.append("lanes placed: the ledger is unreadable")
                unknown = True
            elif request.lanes_placed_last_hour < request.min_lanes_placed_per_hour:
                short.append(
                    f"{request.lanes_placed_last_hour} lanes placed on a pool host in the last "
                    f"hour (bound {request.min_lanes_placed_per_hour})"
                )
        if not short:
            return ModelThroughputResultsResult(alarm=False, state="PRODUCING")
        finding = "; ".join(short)
        cells = (
            f"FRICTION | lane={LANE} | ticket={TICKET} | actor=launchd:{LANE} | "
            f"class=throughput-results | short={len(short)} | "
            f"cost=est ~60 minutes of factory output short of its bound | {finding}; fix: {FIX}"
        )
        return ModelThroughputResultsResult(
            alarm=True,
            state="UNKNOWN" if unknown else "SHORT",
            short=tuple(short),
            fix=FIX,
            friction_cells=cells,
            alert_text=f"THROUGHPUT RESULTS {finding}; fix: {FIX}",
        )
