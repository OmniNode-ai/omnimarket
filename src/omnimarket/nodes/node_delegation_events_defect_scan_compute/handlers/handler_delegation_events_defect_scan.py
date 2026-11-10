# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegationEventsDefectScan: count the three delegation_events defects of a day against the overlay's bounds (OMN-23331).

The rows of the day are those with ``now - window < event_time <= now``. A row dated after the read time is counted apart and
never judged. Only real, non-shadow rows are judged. The scan reads NOT_WORKING when a defect count is over its bound, and also
when fewer rows than the bound's minimum were scanned: an empty day cannot attest that it holds no defect, so a dead lane reads
NOT_WORKING and never a clean zero. Rows that could not be read read UNKNOWN with no count at all.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from omnimarket.models.model_delegation_events_defect_scan import (
    ModelDefectScan,
    ModelDefectScanFact,
    ModelDefectScanOverlay,
    ModelDefectScanRequest,
)
from omnimarket.models.model_reading import EnumReadingVerdict

_NO_NUMBER = "-"


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def reason_token(text: str) -> str:
    """One row cell is one token: no space, pipe or equals sign."""
    return "".join(c if c.isalnum() or c in "-_:." else "-" for c in text.strip())[:120]


def in_window(
    request: ModelDefectScanRequest,
) -> tuple[list[ModelDefectScanFact], int]:
    """The rows inside the day, and how many were dated after the read time."""
    start = request.now - timedelta(seconds=request.overlay.window_seconds)
    inside: list[ModelDefectScanFact] = []
    future = 0
    for event in request.events:
        if event.event_time > request.now:
            future += 1
        elif event.event_time > start:
            inside.append(event)
    return inside, future


def count_defects(
    request: ModelDefectScanRequest, rows: list[ModelDefectScanFact]
) -> tuple[int, int, int, int]:
    """Rows judged, then missing terminal, unread cost recorded as zero and unattributed, over rows already in the day."""
    overlay = request.overlay
    grace_edge = request.now - timedelta(seconds=overlay.terminal_grace_seconds)
    real = [
        r for r in rows if r.data_source == overlay.real_data_source and not r.is_shadow
    ]
    missing = sum(
        1 for r in real if r.terminal_ok is None and r.event_time <= grace_edge
    )
    unread_zero = sum(
        1
        for r in real
        if r.cost_usd == Decimal(0)
        and r.tokens_total > 0
        and not (r.cost_measurement_source or "").strip()
    )
    unattributed = sum(1 for r in real if not (r.caller_lane or "").strip())
    return len(real), missing, unread_zero, unattributed


def judge(
    overlay: ModelDefectScanOverlay,
    scanned: int,
    missing: int,
    unread_zero: int,
    unattributed: int,
) -> list[str]:
    """The bounds the counts break, each named with the number and its bound."""
    bounds = overlay.bounds
    failures: list[str] = []
    if scanned < bounds.min_rows_scanned:
        failures.append(f"rows-scanned-below-bound:{scanned}<{bounds.min_rows_scanned}")
    if missing > bounds.max_missing_terminal:
        failures.append(
            f"missing-terminal-above-bound:{missing}>{bounds.max_missing_terminal}"
        )
    if unread_zero > bounds.max_cost_unread_as_zero:
        failures.append(
            f"cost-unread-as-zero-above-bound:{unread_zero}>{bounds.max_cost_unread_as_zero}"
        )
    if unattributed > bounds.max_unattributed:
        failures.append(
            f"unattributed-above-bound:{unattributed}>{bounds.max_unattributed}"
        )
    return failures


def ledger_row(
    request: ModelDefectScanRequest,
    verdict: EnumReadingVerdict,
    reasons: tuple[str, ...],
    counts: tuple[int, int, int] | None,
    rows_scanned: int,
) -> str:
    """The STATUS row of the scan: the counts and the verdict, or the reason the rows were not read."""
    overlay = request.overlay
    now = stamp(request.now)
    if counts is None:
        shown = (_NO_NUMBER, _NO_NUMBER, _NO_NUMBER)
        outcome = "source-unreadable"
    else:
        shown = (str(counts[0]), str(counts[1]), str(counts[2]))
        outcome = "read"
    cells = [
        f"lane={overlay.row_lane}",
        f"src={overlay.row_src}",
        "drill=no",
        f"outcome={outcome}",
        f"verdict={verdict.value}",
        f"missing_terminal={shown[0]}",
        f"cost_unread_as_zero={shown[1]}",
        f"unattributed={shown[2]}",
        f"rows={rows_scanned}",
        f"window_s={overlay.window_seconds}",
        f"probed_at={now}",
        f"published_at={now}",
        f"max_age={overlay.publish_max_age_seconds}",
        f"reason={reason_token(','.join(reasons)) or 'none'}",
    ]
    return (
        f"{now} | STATUS | "
        + " | ".join(cells)
        + " | delegation_events defect scan of the day"
    )


class HandlerDelegationEventsDefectScan:
    """COMPUTE handler: no clock read (the request carries the time), no state, no I/O."""

    def handle(self, request: ModelDefectScanRequest) -> ModelDefectScan:
        overlay = request.overlay
        if request.events_unread_reason is not None:
            unread: tuple[str, ...] = (
                f"events-unreadable:{reason_token(request.events_unread_reason)}",
            )
            return ModelDefectScan(
                verdict=EnumReadingVerdict.UNKNOWN,
                reasons=unread,
                rows_scanned=0,
                rows_future_dated=0,
                missing_terminal=None,
                cost_unread_as_zero=None,
                unattributed=None,
                window_seconds=overlay.window_seconds,
                read_at=request.now,
                ledger_row=ledger_row(
                    request, EnumReadingVerdict.UNKNOWN, unread, None, 0
                ),
            )
        rows, future = in_window(request)
        scanned, missing, unread_zero, unattributed = count_defects(request, rows)
        reasons = tuple(judge(overlay, scanned, missing, unread_zero, unattributed))
        verdict = (
            EnumReadingVerdict.NOT_WORKING if reasons else EnumReadingVerdict.WORKING
        )
        return ModelDefectScan(
            verdict=verdict,
            reasons=reasons,
            rows_scanned=scanned,
            rows_future_dated=future,
            missing_terminal=missing,
            cost_unread_as_zero=unread_zero,
            unattributed=unattributed,
            window_seconds=overlay.window_seconds,
            read_at=request.now,
            ledger_row=ledger_row(
                request, verdict, reasons, (missing, unread_zero, unattributed), scanned
            ),
        )


__all__: list[str] = [
    "HandlerDelegationEventsDefectScan",
    "count_defects",
    "in_window",
    "judge",
    "ledger_row",
    "reason_token",
]
