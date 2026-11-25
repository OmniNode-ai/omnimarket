# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Name the refusals of ``onex scm pr-create`` in lane rows, and raise one alert for each new cause (OMN-26433).

A lane that cannot open its pull request says so in its STATUS or TERMINAL row: the duplicate guard printed
``PR-DUPLICATE-GUARD unchecked`` (the ledger reader exited 69 after a bus session timeout, or its overlay was not configured), or
``onex scm pr-create`` hung until the lane's own wait ended (rc 124). Four lanes hit it on 2026-11-22 before any alert existed.
This handler reads the rows of a window and returns, for each cause named in a row, the first lane that hit it and one alert,
unless the caller says the cause was already alerted. A cause is named only from the row's own words, and rows of any other kind are never read.
"""

import re

from omnimarket.handlers.pr_create_refusal_causes import (
    CAUSES,
    KINDS,
    REFUSAL,
)
from omnimarket.nodes.node_pr_create_refusal_alert_compute.models import (
    ModelPrCreateRefusalAlert,
    ModelPrCreateRefusalAlertRequest,
    ModelPrCreateRefusalAlertResult,
    ModelPrCreateRefusalClass,
)

_LANE = re.compile(r"(?:^|[\s|])lane=([^\s|]+)")


def _parse(row: str) -> tuple[str, str, str] | None:
    """(stamp, kind, lane) of a ledger row, or None when it is not a lane row."""
    cells = [cell.strip() for cell in row.split(" | ")]
    lane = _LANE.search(row)
    if len(cells) < 3 or lane is None or not re.match(r"\d{4}-\d{2}-\d{2}T", cells[0]):
        return None
    return cells[0], cells[1], lane[1]


class HandlerPrCreateRefusalAlertCompute:
    def handle(
        self, request: ModelPrCreateRefusalAlertRequest
    ) -> ModelPrCreateRefusalAlertResult:
        hits: dict[str, list[tuple[str, str]]] = {c.name: [] for c in CAUSES}
        considered = 0
        for row in request.rows:
            parsed = _parse(row)
            if parsed is None or parsed[1] not in KINDS:
                continue
            considered += 1
            stamp, _, lane = parsed
            if not REFUSAL.search(row):
                continue
            for declared in CAUSES:
                if declared.pattern.search(row):
                    hits[declared.name].append((stamp, lane))
                    break
        says = {c.name: c.says for c in CAUSES}
        classes: list[ModelPrCreateRefusalClass] = []
        alerts: list[ModelPrCreateRefusalAlert] = []
        suppressed: list[str] = []
        for cause, found in hits.items():
            if not found:
                continue
            ordered = sorted(found)
            lanes = tuple(dict.fromkeys(lane for _, lane in ordered))
            first_stamp, first_lane = ordered[0]
            classes.append(
                ModelPrCreateRefusalClass(
                    cause=cause,
                    count=len(found),
                    lanes=lanes,
                    first_lane=first_lane,
                    first_stamp=first_stamp,
                )
            )
            if cause in request.already_alerted:
                suppressed.append(cause)
                continue
            alerts.append(
                ModelPrCreateRefusalAlert(
                    cause=cause,
                    first_lane=first_lane,
                    first_stamp=first_stamp,
                    lanes_so_far=len(lanes),
                    message=(
                        f"pr-create refusal {cause}: {says[cause]}; first hit by lane {first_lane} at {first_stamp}, "
                        f"{len(lanes)} lane(s) so far"
                    ),
                )
            )
        return ModelPrCreateRefusalAlertResult(
            window_start=request.window_start,
            window_end=request.window_end,
            rows_read=len(request.rows),
            rows_considered=considered,
            classes=tuple(sorted(classes, key=lambda c: (-c.count, c.cause))),
            alerts=tuple(alerts),
            suppressed=tuple(suppressed),
        )
