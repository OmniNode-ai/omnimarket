# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reconcile one seam's two sides for one finalized window.

Pure: it takes the seam's declaration and both sides' counts and window closes and returns a
`ModelSeamReading` through `ModelSeamReading.from_crossings`, one crossing per tick.

* unreadable: the sender's close is absent, or the sender handed over at least one crossing and no
  receiver close covers the window, or the budget the overlay keys name was not supplied.
* matched: both residuals are empty, every cause is permitted, none is forbidden, the identity
  artifacts agree and the maximum latency is within the budget. A sender close with no crossings
  and no receiver close is an idle window and reads matched.
* mismatched: otherwise, with the residual, the cause or the identity as the reason.

`dropped` is never self-reported: it is computed as the transit residual (ids handed over that the
receiver never reported; for a coverage measure, ids the receiver required that the inventory lacks)
and the disposition residual (ids the receiver reported with no permitted disposition).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from omnimarket.models.seam_reading import (
    EnumSeamCause,
    EnumSeamCrossingState,
    EnumSeamDisposition,
    EnumSeamMeasureKind,
    ModelSeamCount,
    ModelSeamCrossing,
    ModelSeamIdDisposition,
    ModelSeamMeasureDeclaration,
    ModelSeamReading,
)
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_conservation_request import (
    ModelSeamConservationRequest,
)
from omnimarket.nodes.node_seam_conservation_compute.models.model_seam_conservation_result import (
    ModelSeamConservationResult,
    ModelSeamDerivedCounts,
    ModelSeamResidual,
)

_SHOWN = 3


def _last_emission(
    counts: Iterable[ModelSeamCount], seam_id: str, tick_id: str
) -> dict[str, ModelSeamCount]:
    """One row per crossing, the last emission winning, for crossings that started in this tick."""
    rows: dict[str, ModelSeamCount] = {}
    for count in counts:
        if count.seam_id == seam_id and count.origin_tick_id == tick_id:
            rows[count.crossing_id] = count
    return rows


def _ids(
    rows: dict[str, ModelSeamCount], measure_id: str
) -> list[ModelSeamIdDisposition]:
    found: list[ModelSeamIdDisposition] = []
    for row in rows.values():
        for measure in row.measures:
            if measure.measure_id == measure_id:
                found.extend(measure.ids)
    return found


def _shown(items: Sequence[str]) -> str:
    head = ", ".join(items[:_SHOWN])
    return head + (f" (+{len(items) - _SHOWN})" if len(items) > _SHOWN else "")


def _permitted(
    entry: ModelSeamIdDisposition,
    blocked: frozenset[EnumSeamCause],
    refused: frozenset[EnumSeamCause],
) -> bool:
    match entry.disposition:
        case EnumSeamDisposition.ACCEPTED | EnumSeamDisposition.STALE:
            return True
        case EnumSeamDisposition.BLOCKED:
            return entry.cause in blocked
        case EnumSeamDisposition.REFUSED:
            return entry.cause in refused
        case _:
            return False


def _unreadable(
    request: ModelSeamConservationRequest, reason: str
) -> ModelSeamConservationResult:
    return _result(request, EnumSeamCrossingState.UNREADABLE, reason, (), None, [], [])


def _result(
    request: ModelSeamConservationRequest,
    state: EnumSeamCrossingState,
    reason: str,
    residuals: tuple[ModelSeamResidual, ...],
    max_latency: float | None,
    sent: list[ModelSeamIdDisposition],
    received: list[ModelSeamIdDisposition],
) -> ModelSeamConservationResult:
    reading = ModelSeamReading.from_crossings(
        request.declaration.seam_id,
        request.window,
        [ModelSeamCrossing(key=request.tick_id, state=state, reason=reason)],
        request.read_at,
    )

    def tally(disposition: EnumSeamDisposition) -> int:
        return sum(1 for e in received if e.disposition is disposition)

    return ModelSeamConservationResult(
        state=state,
        reason=reason,
        reading=reading,
        residuals=residuals,
        counts=ModelSeamDerivedCounts(
            out=len(sent),
            received=len(received),
            accepted=tally(EnumSeamDisposition.ACCEPTED),
            stale=tally(EnumSeamDisposition.STALE),
            blocked=tally(EnumSeamDisposition.BLOCKED),
            refused=tally(EnumSeamDisposition.REFUSED),
        ),
        max_latency_seconds=max_latency,
    )


def _reconcile_measure(
    declared: ModelSeamMeasureDeclaration,
    sender: list[ModelSeamIdDisposition],
    receiver: list[ModelSeamIdDisposition],
    blocked: frozenset[EnumSeamCause],
    refused: frozenset[EnumSeamCause],
    forbidden: frozenset[EnumSeamCause],
    latency: list[float],
) -> tuple[ModelSeamResidual, list[str]]:
    problems: list[str] = []
    handed = {
        e.id: e for e in sender if e.disposition is not EnumSeamDisposition.UNUSED
    }
    reported = {e.id: e for e in receiver}
    if declared.kind is EnumSeamMeasureKind.TRANSIT:
        transit = sorted(set(handed) - set(reported))
        transit += [f"{i} (receiver only)" for i in sorted(set(reported) - set(handed))]
        unused: list[str] = []
    else:
        transit = sorted(set(reported) - set(handed))
        unused = sorted(set(handed) - set(reported))
    disposition: list[str] = []
    for entry in receiver:
        if entry.cause in forbidden:
            disposition.append(f"{entry.id}: forbidden cause {entry.cause}")
        elif not _permitted(entry, blocked, refused):
            disposition.append(
                f"{entry.id}: {entry.disposition.value}"
                + (f" ({entry.cause})" if entry.cause else "")
                + " not permitted"
            )
        elif (
            entry.disposition is EnumSeamDisposition.ACCEPTED
            and declared.kind is EnumSeamMeasureKind.TRANSIT
        ):
            sent_at = handed[entry.id].stamp if entry.id in handed else None
            if sent_at is None or entry.stamp is None:
                disposition.append(f"{entry.id}: {EnumSeamCause.LATENCY_INCOMPLETE}")
            else:
                latency.append((entry.stamp - sent_at).total_seconds())
    if transit:
        problems.append(
            f"{declared.measure_id} transit residual {len(transit)}: {_shown(transit)}"
        )
    if disposition:
        problems.append(
            f"{declared.measure_id} disposition residual {len(disposition)}: {_shown(disposition)}"
        )
    return (
        ModelSeamResidual(
            measure_id=declared.measure_id,
            transit=tuple(transit),
            disposition=tuple(disposition),
            unused=tuple(unused),
        ),
        problems,
    )


class HandlerSeamConservation:
    """Stateless compute: one seam's reading for one finalized window."""

    def handle(
        self, request: ModelSeamConservationRequest
    ) -> ModelSeamConservationResult:
        declaration = request.declaration
        sender_close, receiver_close = request.sender_close, request.receiver_close
        if sender_close is None:
            return _unreadable(request, "sender close absent for the window")
        if sender_close.crossing_ids and receiver_close is None:
            return _unreadable(
                request,
                f"receiver close absent while the sender close lists {len(sender_close.crossing_ids)} crossings",
            )
        sender_rows = _last_emission(
            request.sender_counts, declaration.seam_id, request.tick_id
        )
        receiver_rows = _last_emission(
            request.receiver_counts, declaration.seam_id, request.tick_id
        )
        problems: list[str] = []
        for side, rows, window_close in (
            ("sender", sender_rows, sender_close),
            ("receiver", receiver_rows, receiver_close),
        ):
            listed = set(window_close.crossing_ids) if window_close else set()
            unreported = sorted(listed - set(rows))
            unlisted = sorted(set(rows) - listed) if window_close else []
            if unreported:
                problems.append(
                    f"{side} close lists crossings with no count: {_shown(unreported)}"
                )
            if unlisted:
                problems.append(
                    f"{side} counts a crossing its close does not list: {_shown(unlisted)}"
                )
        if sender_close.crossing_ids and request.latency_budget_seconds is None:
            return _unreadable(
                request,
                f"latency budget not supplied from overlay key {declaration.latency_budget_key}",
            )
        blocked = frozenset(declaration.permits_blocked)
        refused = frozenset(declaration.permits_refused)
        forbidden = frozenset(declaration.forbidden_causes)
        residuals: list[ModelSeamResidual] = []
        latency: list[float] = []
        sent_all: list[ModelSeamIdDisposition] = []
        received_all: list[ModelSeamIdDisposition] = []
        declared_ids = {m.measure_id for m in declaration.measures}
        for rows in (sender_rows, receiver_rows):
            for row in rows.values():
                stray = sorted({m.measure_id for m in row.measures} - declared_ids)
                if stray:
                    problems.append(f"undeclared measure {_shown(stray)}")
        for declared in declaration.measures:
            sender_ids = _ids(sender_rows, declared.measure_id)
            receiver_ids = _ids(receiver_rows, declared.measure_id)
            residual, found = _reconcile_measure(
                declared, sender_ids, receiver_ids, blocked, refused, forbidden, latency
            )
            residuals.append(residual)
            problems.extend(found)
            sent_all.extend(sender_ids)
            received_all.extend(receiver_ids)
        for kind in declaration.identity_artifacts:
            sent = {
                a.digest
                for r in sender_rows.values()
                for a in r.artifacts
                if a.kind is kind
            }
            got = {
                a.digest
                for r in receiver_rows.values()
                for a in r.artifacts
                if a.kind is kind
            }
            if (sender_rows or receiver_rows) and (not sent or not got or sent != got):
                problems.append(
                    f"identity {kind.value}: sender {_shown(sorted(sent)) or 'none'}, receiver {_shown(sorted(got)) or 'none'}"
                )
        max_latency = max(latency) if latency else None
        budget = request.latency_budget_seconds
        if max_latency is not None and budget is not None and max_latency > budget:
            problems.append(f"latency {max_latency:g}s over budget {budget:g}s")
        state = (
            EnumSeamCrossingState.MISMATCHED
            if problems
            else EnumSeamCrossingState.MATCHED
        )
        reason = (
            "; ".join(problems)
            if problems
            else ("idle window" if not sender_close.crossing_ids else "matched")
        )
        return _result(
            request,
            state,
            reason,
            tuple(residuals),
            max_latency,
            sent_all,
            received_all,
        )
