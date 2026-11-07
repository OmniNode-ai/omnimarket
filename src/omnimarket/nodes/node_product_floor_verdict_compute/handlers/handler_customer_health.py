# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The customer health line from the floor's own piece readings (OMN-22923, piece 17b; plan section 3.6, join J17).

Pure: the readings, the time and the overlay are handed in; nothing is read here. For each piece the overlay names, the
line judges the reading by its age against that reading's own maximum age, so a reading older than its limit is NOT
WORKING and is named, whatever state it still carries. A piece with no reading is UNKNOWN, never WORKING. The line is
WORKING only when every required reading is fresh and WORKING.
"""

from __future__ import annotations

from omnimarket.models.floor_17a import (
    ModelFloorHealthLine,
    ModelHealthReadingUsed,
    ReadingState,
)
from omnimarket.nodes.node_product_floor_verdict_compute.models.model_customer_health import (
    ModelCustomerHealthRequest,
    ModelHealthReadingIn,
)


def _minutes(seconds: int) -> str:
    return f"{round(seconds / 60)} min"


class HandlerCustomerHealth:
    """The customer health line, from readings."""

    @staticmethod
    def _too_old(age_s: int, max_age_s: int) -> bool:
        return age_s > max_age_s

    def handle(self, request: ModelCustomerHealthRequest) -> ModelFloorHealthLine:
        by_piece = {r.piece: r for r in request.readings}
        used: list[ModelHealthReadingUsed] = []
        named: list[str] = []
        states: list[ReadingState] = []
        for piece in request.overlay.required_pieces:
            row = by_piece.get(piece)
            if row is None:
                used.append(
                    ModelHealthReadingUsed(
                        piece=piece,
                        name="",
                        state=None,
                        age_s=None,
                        max_age_s=None,
                        verdict="unread",
                    )
                )
                named.append(f"piece {piece}: no reading")
                states.append("UNKNOWN")
                continue
            verdict, why = self._judge(row)
            used.append(
                ModelHealthReadingUsed(
                    piece=piece,
                    name=row.name,
                    state=row.reading_state,
                    age_s=row.reading_age_s,
                    max_age_s=row.reading_max_age_s,
                    verdict=verdict,
                )
            )
            if verdict == "fresh":
                states.append("WORKING")
                continue
            states.append("UNKNOWN" if verdict == "unread" else "NOT WORKING")
            named.append(f"piece {piece} {row.name}: {why}")
        state: ReadingState = (
            "NOT WORKING"
            if "NOT WORKING" in states
            else "UNKNOWN"
            if "UNKNOWN" in states
            else "WORKING"
        )
        ages = [r.age_s for r in used if r.age_s is not None]
        oldest = max(ages) if ages else None
        return ModelFloorHealthLine(
            state=state,
            line=self._words(request, state, tuple(named), oldest),
            generated_at=request.now,
            max_age_s=request.overlay.max_age_s,
            readings=tuple(used),
            named=tuple(named),
            oldest_age_s=oldest,
        )

    def _judge(self, row: ModelHealthReadingIn) -> tuple[str, str]:
        """The verdict on one reading and the reason, judged by age first."""
        age, limit = row.reading_age_s, row.reading_max_age_s
        if row.reading_state is None or age is None or limit is None:
            return "unread", "not read"
        if age < 0:
            return "failing", f"read dated in the future ({-age} s ahead)"
        if self._too_old(age, limit):
            return "stale", f"stale, read {_minutes(age)} ago, limit {_minutes(limit)}"
        if row.reading_state == "NOT WORKING":
            return "failing", "reading NOT WORKING"
        if row.reading_state == "UNKNOWN":
            return "unread", "reading UNKNOWN"
        return "fresh", ""

    @staticmethod
    def _words(
        request: ModelCustomerHealthRequest,
        state: ReadingState,
        named: tuple[str, ...],
        oldest: int | None,
    ) -> str:
        title = request.overlay.title
        if state == "WORKING":
            return (
                f"{title}: WORKING, {len(request.overlay.required_pieces)} readings, "
                f"oldest reading {_minutes(oldest or 0)}"
            )
        return f"{title}: {state}, " + "; ".join(named)
