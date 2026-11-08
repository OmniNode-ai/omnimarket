# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationCompletionRate: the delegation completion rate of a window against the overlay's bounds (OMN-23108).

The rows of the window are those with ``now - window < event_time <= now``. A row dated after the read time is counted apart and
never judged. The reading is NOT_WORKING when ``R_all`` or ``R_dep`` is under its bound or ``failed_rate`` is over its bound: an
empty window is under both bounds, so a dead lane reads NOT_WORKING and never reads as a zero that passes. Rows that could not be
read read UNKNOWN, never zero. The reading is carried to the floor as one STATUS ledger row the floor's ``ledger_shadows`` rule
declares as its source.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from omnimarket.models.model_reading import EnumReadingVerdict
from omnimarket.nodes.node_delegation_completion_rate_compute.models.model_delegation_completion_rate import (
    ModelDelegationCompletionRate,
    ModelDelegationCompletionRateRequest,
    ModelDelegationEventFact,
    ModelDelegationRateOverlay,
)

_NO_NUMBER = "-"


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def reason_token(text: str) -> str:
    """One row cell is one token: no space, pipe or equals sign."""
    return "".join(c if c.isalnum() or c in "-_:." else "-" for c in text.strip())[:120]


def in_window(
    request: ModelDelegationCompletionRateRequest,
) -> tuple[list[ModelDelegationEventFact], int]:
    """The rows inside the window, and how many were dated after the read time."""
    start = request.now - timedelta(seconds=request.overlay.window_seconds)
    inside: list[ModelDelegationEventFact] = []
    future = 0
    for event in request.events:
        if event.event_time > request.now:
            future += 1
        elif event.event_time > start:
            inside.append(event)
    return inside, future


def count_rates(
    overlay: ModelDelegationRateOverlay, rows: list[ModelDelegationEventFact]
) -> tuple[int, int, float | None]:
    """``R_all``, ``R_dep`` and ``failed_rate`` of rows already inside the window."""
    real = [
        r for r in rows if r.data_source == overlay.real_data_source and not r.is_shadow
    ]
    completed = [
        r for r in real if r.terminal_ok is True and r.quality_gate_passed is True
    ]
    deployed = [
        r
        for r in completed
        if r.answering_backend is not None
        and not r.answering_backend.startswith(overlay.harness_backend_prefix)
    ]
    failed = sum(1 for r in real if r.terminal_ok is not True)
    return len(completed), len(deployed), (failed / len(real) if real else None)


def judge(
    overlay: ModelDelegationRateOverlay,
    r_all: int,
    r_dep: int,
    failed_rate: float | None,
) -> list[str]:
    """The bounds the numbers break, each named with the number and its bound."""
    bounds = overlay.bounds
    failures: list[str] = []
    if r_all < bounds.min_r_all:
        failures.append(f"r_all-below-bound:{r_all}<{bounds.min_r_all}")
    if r_dep < bounds.min_r_dep:
        failures.append(f"r_dep-below-bound:{r_dep}<{bounds.min_r_dep}")
    if failed_rate is not None and failed_rate > bounds.max_failed_rate:
        failures.append(
            f"failed_rate-above-bound:{failed_rate:.4f}>{bounds.max_failed_rate:.4f}"
        )
    return failures


def ledger_row(
    request: ModelDelegationCompletionRateRequest,
    verdict: EnumReadingVerdict,
    reasons: tuple[str, ...],
    numbers: tuple[int, int, float | None] | None,
    rows_in_window: int,
) -> str:
    """The STATUS row the floor's declared source reads: numbers and verdict, or the reason the rows were not read."""
    overlay = request.overlay
    now = stamp(request.now)
    if numbers is None:
        shown = (_NO_NUMBER, _NO_NUMBER, _NO_NUMBER)
        outcome = "source-unreadable"
    else:
        r_all, r_dep, rate = numbers
        shown = (str(r_all), str(r_dep), _NO_NUMBER if rate is None else f"{rate:.4f}")
        outcome = "read"
    cells = [
        f"lane={overlay.row_lane}",
        f"src={overlay.row_src}",
        "drill=no",
        f"outcome={outcome}",
        f"verdict={verdict.value}",
        f"r_all={shown[0]}",
        f"r_dep={shown[1]}",
        f"failed_rate={shown[2]}",
        f"rows={rows_in_window}",
        f"window_s={overlay.window_seconds}",
        f"probed_at={now}",
        f"published_at={now}",
        f"max_age={overlay.publish_max_age_seconds}",
        f"reason={reason_token(','.join(reasons)) or 'none'}",
    ]
    return (
        f"{now} | STATUS | "
        + " | ".join(cells)
        + " | delegation completion rate over delegation_events"
    )


class HandlerDelegationCompletionRate:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(
        self, request: ModelDelegationCompletionRateRequest
    ) -> ModelDelegationCompletionRate:
        overlay = request.overlay
        if request.events_unread_reason is not None:
            unread: tuple[str, ...] = (
                f"events-unreadable:{reason_token(request.events_unread_reason)}",
            )
            return ModelDelegationCompletionRate(
                verdict=EnumReadingVerdict.UNKNOWN,
                reasons=unread,
                r_all=None,
                r_dep=None,
                failed_rate=None,
                rows_in_window=0,
                rows_future_dated=0,
                window_seconds=overlay.window_seconds,
                read_at=request.now,
                ledger_row=ledger_row(
                    request, EnumReadingVerdict.UNKNOWN, unread, None, 0
                ),
            )
        rows, future = in_window(request)
        r_all, r_dep, failed_rate = count_rates(overlay, rows)
        reasons = tuple(judge(overlay, r_all, r_dep, failed_rate))
        verdict = (
            EnumReadingVerdict.NOT_WORKING if reasons else EnumReadingVerdict.WORKING
        )
        return ModelDelegationCompletionRate(
            verdict=verdict,
            reasons=reasons,
            r_all=r_all,
            r_dep=r_dep,
            failed_rate=failed_rate,
            rows_in_window=len(rows),
            rows_future_dated=future,
            window_seconds=overlay.window_seconds,
            read_at=request.now,
            ledger_row=ledger_row(
                request, verdict, reasons, (r_all, r_dep, failed_rate), len(rows)
            ),
        )


__all__: list[str] = [
    "HandlerDelegationCompletionRate",
    "count_rates",
    "in_window",
    "judge",
    "ledger_row",
]
