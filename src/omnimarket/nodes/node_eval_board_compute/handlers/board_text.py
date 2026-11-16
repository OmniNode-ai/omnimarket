# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Plain-words helpers of the Model tests tab (OMN-24174): a rate as a sentence, never a bare percentage."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_overlay import (
    ID_SHAPED,
)
from omnimarket.nodes.node_eval_board_compute.models.model_eval_board_record import (
    ModelEvalBoardRate,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_snapshot import (
    ModelEvalSnapshotLiveWeek,
    ModelEvalSnapshotRate,
)

SIZE_WORDS: dict[str, str] = {"S": "small", "M": "medium", "L": "large"}
SIZE_ORDER: tuple[str, ...] = ("S", "M", "L")
SIZE_BAND_ALL = "all"


def percent(passed: int, n: int) -> int:
    """``passed`` of ``n`` as a whole percent, half rounded up, in integers."""
    return (200 * passed + n) // (2 * n)


def percent_of(fraction: float) -> int:
    return int(fraction * 100 + 0.5)


def rate_of(
    row: ModelEvalSnapshotRate | ModelEvalSnapshotLiveWeek, min_n_point: int
) -> ModelEvalBoardRate:
    """One rate row as the tab reads it: count and range, or the count with too-few-tasks wording."""
    n, passed = row.n, row.passed
    if n < min_n_point:
        return ModelEvalBoardRate(
            n=n,
            passed=passed,
            percent=None,
            likely_low=None,
            likely_high=None,
            too_few=True,
            text=f"passed {passed} of {n} tasks, too few tasks to read",
        )
    pct = percent(passed, n)
    low, high = percent_of(row.wilson_low), percent_of(row.wilson_high)
    return ModelEvalBoardRate(
        n=n,
        passed=passed,
        percent=pct,
        likely_low=low,
        likely_high=high,
        too_few=False,
        text=f"passed {passed} of {n} tasks ({pct}%, likely {low} to {high}%)",
    )


def plain(text: str) -> str:
    """``text`` as words: any id-shaped token dropped, then underscores read as spaces.

    The id-shaped tokens are matched first: ``node_`` is one of them, and it is gone once the
    underscores are spaces.
    """
    words = [w for w in text.split() if not ID_SHAPED.search(w)]
    return " ".join(words).replace("_", " ")


_LIVE_WEEK = re.compile(r"^(?:live-)?(\d{4})-W(\d{2})$")


def week_date(week: ModelEvalSnapshotLiveWeek) -> str:
    """The Monday of a live week as a date, ``2026-11-09``; never the live run id (``live-2026-W46``).

    A week that is not an ISO week reads as the day its work was first scored.
    """
    match = _LIVE_WEEK.match(week.week)
    if match is not None:
        try:
            return date.fromisocalendar(
                int(match.group(1)), int(match.group(2)), 1
            ).isoformat()
        except ValueError:
            pass
    return day(week.first_scored_at)


def day(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%d")


def minute(moment: datetime) -> str:
    return _utc(moment).strftime("%Y-%m-%d %H:%M")


def _utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


__all__ = [
    "SIZE_BAND_ALL",
    "SIZE_ORDER",
    "SIZE_WORDS",
    "day",
    "minute",
    "percent",
    "percent_of",
    "plain",
    "rate_of",
    "week_date",
]
