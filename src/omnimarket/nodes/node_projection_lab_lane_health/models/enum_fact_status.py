"""Per-fact verdict vocabulary and the staleness decay (OMN-18769).

The decay is the archived subsystem view's two-field ``status`` /
``originalStatus`` model, carried here on purpose. The archive's own failure was
the opposite shape -- one row-level ``updated_at`` over facts of wildly
different ages -- and a day-old green then read as green.

Two rules, and the second is the one that gets dropped in re-implementations:

1. A PASS decays. At 8 hours it reads WARN; at 24 hours it reads STALE.
2. The PRE-DECAY verdict is preserved alongside it, so a reader can say
   "was PASS, now stale" rather than having to choose between a lie and
   an erasure.

A verdict that is already FAIL does **not** decay upward or downward. Age does
not repair a failure and does not deepen it: the fact is simply old, and the
age is on the row for a reader to judge.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

#: A PASS older than this reads WARN. Archived value, kept: it is roughly a
#: working day, which is the interval after which "it was fine this morning"
#: stops being an answer.
WARN_AFTER = timedelta(hours=8)

#: A PASS older than this reads STALE -- no longer a claim about now at all.
STALE_AFTER = timedelta(hours=24)


class EnumFactStatus(StrEnum):
    """The verdict one contributing fact carries."""

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    #: The fact exists but is too old to be a statement about the present.
    STALE = "STALE"
    #: No fact has ever arrived for this dimension on this lane. Distinct from
    #: STALE, which means one arrived and aged out, and distinct from PASS,
    #: which some callers reach for when a producer is simply absent. A panel
    #: renders UNKNOWN as "unproduced", never as green.
    UNKNOWN = "UNKNOWN"


def decay(
    original: EnumFactStatus,
    observed_at: datetime | None,
    *,
    now: datetime,
) -> EnumFactStatus:
    """Return the age-adjusted verdict for one fact.

    Args:
        original: The verdict as the producing surface stated it.
        observed_at: When the producing surface observed it. ``None`` means the
            fact carries no timestamp, which is not an excuse to treat it as
            fresh -- it resolves UNKNOWN.
        now: The evaluation instant, passed in rather than read from the clock
            so the decay is a pure function and a test can state an age.

    A future-dated ``observed_at`` (clock skew between a lab host and the
    reducer) is treated as age zero rather than as an error: a skewed clock is
    a reason to distrust freshness, never a reason to drop a fact.
    """
    if observed_at is None:
        return EnumFactStatus.UNKNOWN
    if original in (EnumFactStatus.UNKNOWN, EnumFactStatus.FAIL, EnumFactStatus.STALE):
        return original
    age = now - observed_at
    if age >= STALE_AFTER:
        return EnumFactStatus.STALE
    if age >= WARN_AFTER:
        return EnumFactStatus.WARN if original is EnumFactStatus.PASS else original
    return original
