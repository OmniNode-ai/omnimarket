# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A wall-clock budget for one merge-event ``dod_verify`` dispatch (OMN-26310).

The evidence collector bounds every external call it makes on its own: 30 s for
a check command, 300 s for a git operation, 900 s for a lock-exact environment
build. Nothing bounded their SUM, so one dispatch for
``onex.evt.github.pr-merged.v1`` ran 810 to 948 s against the 600 s dispatch
deadline on the dev runtime (offsets 6659, 6660, 6661, 2026-11-23).

This module is the missing bound. The on-merge handler opens a budget around
its verification; the collector then

* refuses to START a check once the budget is spent, recording it SKIPPED with
  ``CHECK_BUDGET_EXCEEDED`` (never a pass, still blocks a Done flip), and
* clamps the ceiling of every subprocess it does start to the time left, so no
  call runs past the budget.

A verification run with no open budget (a start command, the local Done gate,
the closeout sweep) is unchanged: :func:`clamp_to_budget` returns its argument.
The dispatch deadline itself is not touched.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# Operator override for the on-merge budget, read per call.
ON_MERGE_BUDGET_ENV = "DOD_VERIFY_ON_MERGE_BUDGET_S"

# Half the 600 s dispatch deadline: leaves the other half for the publish of the
# verdict, the projection write and a loaded host, and for the one in-flight
# call that the clamp cannot shorten below its own start-up.
DEFAULT_ON_MERGE_BUDGET_S = 300.0

# The floor of a clamped ceiling: a subprocess given less than this cannot
# start, so the budget is treated as spent instead.
_MIN_CALL_S = 1.0

DISPATCH_BUDGET_MARKER = "DISPATCH_BUDGET_EXCEEDED:"


def on_merge_budget_s() -> float:
    """Resolve the on-merge budget; malformed or non-positive uses the default.

    Never disables the bound: a value that cannot be honoured resolves to the
    default, as ``_check_timeout_s`` does for its own ceiling.
    """
    raw = os.environ.get(ON_MERGE_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_ON_MERGE_BUDGET_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_ON_MERGE_BUDGET_S
    return value if value > 0 else DEFAULT_ON_MERGE_BUDGET_S


def _now() -> float:
    return time.monotonic()


class DispatchBudget:
    """A monotonic deadline ``total_s`` seconds after construction."""

    def __init__(self, total_s: float, *, clock: Callable[[], float] = _now) -> None:
        self.total_s = total_s
        self._clock = clock
        self._deadline = clock() + total_s

    def remaining_s(self) -> float:
        return max(0.0, self._deadline - self._clock())

    def exhausted(self) -> bool:
        return self.remaining_s() < _MIN_CALL_S


_ACTIVE: ContextVar[DispatchBudget | None] = ContextVar(
    "dod_verify_dispatch_budget", default=None
)


@contextmanager
def dispatch_budget(total_s: float) -> Iterator[DispatchBudget]:
    """Open a budget for the verification run inside the ``with`` block."""
    budget = DispatchBudget(total_s)
    token = _ACTIVE.set(budget)
    try:
        yield budget
    finally:
        _ACTIVE.reset(token)


def budget_exhausted() -> bool:
    """True when a budget is open and has no usable time left."""
    budget = _ACTIVE.get()
    return budget is not None and budget.exhausted()


def clamp_to_budget(ceiling_s: float) -> float:
    """The call's own ceiling, cut to the time the open budget has left."""
    budget = _ACTIVE.get()
    if budget is None:
        return ceiling_s
    return min(ceiling_s, max(budget.remaining_s(), _MIN_CALL_S))


def exhausted_message() -> str:
    """The refusal text for a check the spent budget did not start."""
    budget = _ACTIVE.get()
    total = f"{budget.total_s:g}s " if budget is not None else ""
    return (
        f"{DISPATCH_BUDGET_MARKER} this merge-event dispatch's {total}"
        f"wall-clock budget ({ON_MERGE_BUDGET_ENV}) was spent before this "
        "check started, so it was NOT run and this is not a statement about "
        "the ticket. A dod-verify start command runs it without this budget. "
        "This still blocks a Done-flip."
    )


__all__: list[str] = [
    "DEFAULT_ON_MERGE_BUDGET_S",
    "DISPATCH_BUDGET_MARKER",
    "ON_MERGE_BUDGET_ENV",
    "DispatchBudget",
    "budget_exhausted",
    "clamp_to_budget",
    "dispatch_budget",
    "exhausted_message",
    "on_merge_budget_s",
]
