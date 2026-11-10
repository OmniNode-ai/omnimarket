# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settle each lane of a run into one state, count the placed ones and name every lane that did not start (OMN-23025).

The fill wrote its STATUS row while its detached send jobs were still building, so a lane
that had not yet started read pending and was not counted placed, and the refusals cell
listed only lanes a receipt had classified: a lane the dispatch never started (a skipped
brief, a lane cut at its slice) was counted in the states cell and named nowhere. This
decides both from the placements alone: a pending lane with a CLAIM on a host is placed, a
pending lane without one is still preparing, and every lane the dispatch never started and
every refused lane is named with its reason in the refusals cell.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from ..models.model_lab_fill_lane_states import (
    ModelLabFillLaneState,
    ModelLabFillLaneStatesRequest,
    ModelLabFillLaneStatesResult,
)
from .handler_lab_fill_status import cell
from .helpers_outcomes import text

_SKIPPED = "skipped:"
_NOT_STARTED = "not-started"

# OMN-23025: the send job labels every brief-build exit other than the check's refusal (3) as
# brief-rules-unreadable, whichever step failed. On 2026-11-08 that one label covered Linear's
# HTTP 429 in the ticket reads (07:50Z fire 29 lanes, 08:10Z fire 22) and the absent ticket_mirror
# overlay (09:10Z to 09:50Z fires, 8 lanes). The job's log tail rides in the placement's detail;
# these rules read it, the rules reader's own failures first because it runs first.
_CATCH_ALL = "brief-rules-unreadable"
_RULES_FILE = re.compile(r"(/[^\s:]*lane-rules-block\.md)")
_RULES_CHECKOUT = re.compile(r"resolver: no valid \S+ clone")
_RULES_MARKER = re.compile(r"brief standing rules (?:lack|heading is missing)")
_RULES_SCRIPT = re.compile(r"lane_rules_block")
_RATE_LIMIT = re.compile(r"HTTP(?: Error)? 429\b|Too Many Requests", re.IGNORECASE)
_MIRROR = re.compile(r"ticket_mirror overlay")


def name_skip_cause(reason: str, detail: str) -> str:
    """The catch-all skip reason renamed for the step its log tail shows failed; any other reason is kept.

    A detail no rule knows keeps the dispatch's label, so a new failure is never given a
    cause it does not show.
    """
    if reason != _CATCH_ALL or not detail:
        return reason
    if match := _RULES_FILE.search(detail):
        return f"{_CATCH_ALL}:{match.group(1)}"
    if _RULES_CHECKOUT.search(detail):
        return f"{_CATCH_ALL}:rules-checkout"
    if _RULES_MARKER.search(detail):
        return f"{_CATCH_ALL}:standing-rules-marker"
    if _RULES_SCRIPT.search(detail):
        return f"{_CATCH_ALL}:lane_rules_block.py"
    if _RATE_LIMIT.search(detail):
        return "brief-ticket-rate-limited"
    if _MIRROR.search(detail):
        return "brief-mirror-overlay-missing"
    return reason


def settle_lane(
    placement: Mapping[str, object], claimed: frozenset[str]
) -> ModelLabFillLaneState:
    """The lane's state: placed, preparing, never-started, refused or other, with its reason."""
    lane = text(placement.get("lane"))
    host = text(placement.get("host"))
    outcome = text(placement.get("outcome"))
    if outcome in ("placed", "placed-elsewhere"):
        return ModelLabFillLaneState(lane=lane, host=host, state="placed")
    if outcome == "pending":
        return ModelLabFillLaneState(
            lane=lane, host=host, state="placed" if lane in claimed else "preparing"
        )
    if outcome == _NOT_STARTED:
        return ModelLabFillLaneState(
            lane=lane, host=host, state="never-started", reason=_NOT_STARTED
        )
    if outcome.startswith(_SKIPPED):
        return ModelLabFillLaneState(
            lane=lane,
            host=host,
            state="never-started",
            reason=name_skip_cause(
                outcome[len(_SKIPPED) :], text(placement.get("detail"))
            ),
        )
    if outcome == "refused" or outcome.startswith(("engine-failed:", "deferred:")):
        return ModelLabFillLaneState(
            lane=lane, host=host, state="refused", reason=outcome
        )
    return ModelLabFillLaneState(
        lane=lane, host=host, state="other", reason=outcome or "unknown"
    )


class HandlerLabFillLaneStates:
    """Pure: the same placements and claims always give the same cells."""

    def handle(
        self, request: ModelLabFillLaneStatesRequest
    ) -> ModelLabFillLaneStatesResult:
        claimed = frozenset(request.claimed_lanes)
        lanes = tuple(
            settle_lane(p, claimed) for p in (*request.placements, *request.fallbacks)
        )
        counts: dict[str, int] = {}
        for lane in lanes:
            counts[lane.state] = counts.get(lane.state, 0) + 1
        refusals = [
            _refusal(lane)
            for lane in lanes
            if lane.state in ("never-started", "refused", "other")
        ]
        return ModelLabFillLaneStatesResult(
            states_cell="states="
            + cell(",".join(f"{k}:{n}" for k, n in counts.items()) or "none"),
            refusals_cell="refusals=" + cell(",".join(refusals) or "none"),
            placed=counts.get("placed", 0),
            never_started=tuple(
                lane.lane for lane in lanes if lane.state == "never-started"
            ),
            lanes=lanes,
        )


def _refusal(lane: ModelLabFillLaneState) -> str:
    """``lane@host:reason`` as the tick node writes it; an outcome no state names keeps ``other:``."""
    reason = f"other:{lane.reason}" if lane.state == "other" else lane.reason
    return f"{lane.lane}@{lane.host or 'none'}:{reason}"
