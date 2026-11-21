# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared bounds of one eval-results delivery, and the typed refusals that name a breach.

The runtime abandons a dispatch that is still running at its 600 s deadline, and three abandoned
dispatches turn the bus unhealthy. A delivery to this writer therefore fails or returns inside the
bounds below, which sit far under that deadline, and says which stage held it. The bounds are
constants of the node, not overlay keys: a deployment overlay has no key for them and must not need
one, and a tick that cannot be bounded by configuration it forgot to supply is the defect itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, Final

#: One awaited stage (connect, snapshot read, publish transport, publish) may hold a delivery this long.
STAGE_DEADLINE_S: Final[float] = 15.0
#: The whole delivery, every stage and the release of what it held, is bounded by this.
DISPATCH_DEADLINE_S: Final[float] = 45.0
#: Deliveries of one record that meet an unmaterialized table before the record is dead-lettered.
#: The platform's own redelivery bound is 5; this node dead-letters first, with its own typed reason.
NOT_MATERIALIZED_MAX_DELIVERIES: Final[int] = 3
#: Records whose delivery count is held at once; the oldest is dropped past this, a leak bound only.
NOT_MATERIALIZED_TRACKED_RECORDS: Final[int] = 1024

REFUSAL_DEADLINE: Final[str] = "deadline"
REFUSAL_NOT_MATERIALIZED: Final[str] = "not_materialized"

_UNDEFINED_TABLE_SQLSTATE: Final[str] = "42P01"


class EvalResultsDeadlineError(RuntimeError):
    """A stage of one delivery did not complete inside its bound."""

    def __init__(self, stage: str, limit_s: float) -> None:
        super().__init__(
            f"eval-results stage {stage!r} did not complete within {limit_s}s"
        )
        self.stage = stage
        self.limit_s = limit_s

    def refusal(self) -> dict[str, Any]:
        return {
            "published": False,
            "refused": REFUSAL_DEADLINE,
            "stage": self.stage,
            "limit_s": self.limit_s,
        }


class EvalResultsNotMaterializedError(RuntimeError):
    """The projection table the delivery needs is not materialized; the offset must not advance yet."""

    def __init__(self, detail: str, *, delivery: int, limit: int) -> None:
        super().__init__(
            f"eval-results projection table is not materialized (delivery {delivery} of {limit}): {detail}"
        )
        self.delivery = delivery
        self.limit = limit


class EvalResultsDeadLetterNotPublishedError(RuntimeError):
    """A record owed a dead letter did not get one confirmed published; the offset must not advance."""

    def __init__(self, subject: str, reason: str) -> None:
        super().__init__(f"dead letter not published for {subject}: {reason}")
        self.subject = subject


async def bounded[T](stage: str, awaitable: Awaitable[T], limit_s: float) -> T:
    """Await ``awaitable`` for at most ``limit_s`` seconds; a breach is a typed refusal naming ``stage``."""
    try:
        return await asyncio.wait_for(awaitable, limit_s)
    except TimeoutError as exc:
        raise EvalResultsDeadlineError(stage, limit_s) from exc


def is_not_materialized(exc: BaseException) -> bool:
    """True when ``exc`` or a cause of it is a relation that does not exist (SQLSTATE 42P01)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "sqlstate", None) == _UNDEFINED_TABLE_SQLSTATE:
            return True
        if type(current).__name__ == "UndefinedTableError":
            return True
        current = current.__cause__ or current.__context__
    return False


__all__ = [
    "DISPATCH_DEADLINE_S",
    "NOT_MATERIALIZED_MAX_DELIVERIES",
    "NOT_MATERIALIZED_TRACKED_RECORDS",
    "REFUSAL_DEADLINE",
    "REFUSAL_NOT_MATERIALIZED",
    "STAGE_DEADLINE_S",
    "EvalResultsDeadLetterNotPublishedError",
    "EvalResultsDeadlineError",
    "EvalResultsNotMaterializedError",
    "bounded",
    "is_not_materialized",
]
