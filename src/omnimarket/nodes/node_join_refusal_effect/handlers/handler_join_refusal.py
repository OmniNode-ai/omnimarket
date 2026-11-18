# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The downstream piece's typed refusal when the upstream piece failed (OMN-23022, the floor's chains:broken row).

A join A to B is golden when A succeeded and B succeeded after it; it is an error join when A failed and B holds a typed
``upstream_`` failure and never started. The floor builds A's failure chains from ledger rows (the host-canary reading
that reads NOT WORKING is a failure of piece 2, under a correlation id the row decides). Nothing downstream ever answered
them: piece 3 read nothing from piece 2, so after the chain deadline each failure was a ``downstream_missing_typed_failure``
and the floor's ``chains:broken`` row stayed red.

This node is that answer. For each join the overlay declares it reads the upstream piece's real failure chains (the same
rule and the same rows the floor reads, so the same correlation ids), and writes to the emission journal one typed
``upstream_<code>`` failure, with no start, of the downstream piece under each id the downstream piece has not answered.
Three things it does not do: it never answers a drill (a drill is not evidence for a join), it never answers a failure
twice within the journal window (``emission.window_hours``; an answer older than it is not read back, so a failure still
inside its upstream lookback is answered again), and it never answers a success. It changes no verdict and no bound; with the refusal dropped the join reads broken
again (``injected_fault`` in the overlay names that break).

Canonical definition-B shape: ``handle(ModelJoinRefusalCommand) -> ModelJoinRefusalResult``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_join_refusal_effect.models import (
    ModelJoinRefusalCommand,
    ModelJoinRefusalReading,
    ModelJoinRefusalResult,
)
from omnimarket.nodes.node_join_refusal_effect.overlay import (
    ModelJoinRefusalOverlay,
    ModelRefusedJoin,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

UPSTREAM_PREFIX = "upstream_"
_FUTURE_SLACK = timedelta(days=1)


def _journal_failures(
    journal: tuple[ModelNodeEmission, ...], piece: int, nodes: frozenset[str]
) -> tuple[list[ModelNodeEmission], int]:
    """The real failure chains the named nodes of ``piece`` wrote to the journal, and how many drill chains were left out.

    A chain is every event of one node under one correlation id. It is a real failure when it holds a typed failure and is
    not a drill. A chain that only refused (a typed ``upstream_`` failure and no start) is the answer to an earlier failure,
    never a failure of its own, so it is not answered: the floor reads it the same way.
    """
    chains: dict[tuple[str, str], list[ModelNodeEmission]] = {}
    for event in journal:
        if event.piece == piece and event.node in nodes:
            chains.setdefault((event.node, event.correlation_id), []).append(event)
    real: list[ModelNodeEmission] = []
    drills = 0
    for events in chains.values():
        fails = [e for e in events if e.phase == "failure"]
        if not fails:
            continue
        started = any(e.phase == "start" for e in events)
        refused = not started and all(
            (f.failure_code or "").startswith(UPSTREAM_PREFIX) for f in fails
        )
        if refused:
            continue
        if any(e.drill for e in events):
            drills += 1
            continue
        real.append(min(fails, key=lambda e: e.emitted_at))
    return real, drills


class HandlerJoinRefusal:
    """Answer each real upstream failure of the declared joins with the downstream piece's typed refusal."""

    def __init__(
        self,
        *,
        overlay: ModelJoinRefusalOverlay,
        source: ProtocolLedgerRowSource,
        sink: ProtocolEmissionSink,
        backend: Literal["in_memory", "real"],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._source = source
        self._sink = sink
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelJoinRefusalCommand) -> ModelJoinRefusalResult:
        now = self._clock()
        since = now - timedelta(hours=self._overlay.emission.window_hours)
        journal = self._sink.read(since)
        readings: list[ModelJoinRefusalReading] = []
        wrote = False
        for join in self._overlay.joins:
            if request.only_joins and join.join not in request.only_joins:
                continue
            reading, refusals = self._read_join(join, journal, now)
            # A later join over the same upstream failure sees this join's refusal as the answer.
            journal = (*journal, *refusals)
            if request.write and refusals:
                for refusal in refusals:
                    self._sink.emit(refusal)
                wrote = True
                reading = reading.model_copy(update={"written": len(refusals)})
            readings.append(reading)
        return ModelJoinRefusalResult(
            ok=all(r.status != "unread" for r in readings),
            wrote=wrote,
            readings=tuple(readings),
        )

    def _read_join(
        self,
        join: ModelRefusedJoin,
        journal: tuple[ModelNodeEmission, ...],
        now: datetime,
    ) -> tuple[ModelJoinRefusalReading, list[ModelNodeEmission]]:
        rule = join.upstream
        read = self._source.read(
            ModelLedgerQuery(
                since=now - timedelta(days=rule.lookback_days),
                until=now + _FUTURE_SLACK,
                row_type=rule.error.row_type,
                lane=rule.error.lane,
            )
        )
        base = {
            "join": join.join,
            "from_piece": join.from_piece,
            "to_piece": join.to_piece,
        }
        if read.error is not None:
            # An unreadable ledger is never an empty answer: nothing is owed or written, and the run says so.
            return (
                ModelJoinRefusalReading(
                    **base, status="unread", reason=f"ledger unread: {read.error}"
                ),
                [],
            )
        events = piece_events(list(read.rows), (rule,))
        failures = [
            e for e in events if e.phase == "failure" and e.piece == join.from_piece
        ]
        real = [e for e in failures if not e.drill]
        drills_skipped = len(failures) - len(real)
        if join.journal_upstream is not None:
            journal_real, journal_drills = _journal_failures(
                journal, join.from_piece, frozenset(join.journal_upstream.nodes)
            )
            real = [*real, *journal_real]
            drills_skipped += journal_drills
        answered_ids = {e.correlation_id for e in journal if e.piece == join.to_piece}
        refusals: list[ModelNodeEmission] = []
        answered = 0
        seen: set[str] = set()
        for failure in real:
            cid = failure.correlation_id
            if cid in seen:
                continue
            seen.add(cid)
            if cid in answered_ids:
                answered += 1
                continue
            refusals.append(
                ModelNodeEmission.model_validate(
                    {
                        "node": join.downstream_node,
                        "piece": join.to_piece,
                        "correlation_id": cid,
                        "phase": "failure",
                        "emitted_at": now,
                        "backend": self._backend,
                        "origin": "probe",
                        "failure_code": f"{UPSTREAM_PREFIX}{failure.failure_code}",
                        "failure_detail": (
                            f"{join.join}: piece {join.from_piece} failed with "
                            f"{failure.failure_code}; piece {join.to_piece} refuses and does not start"
                        ),
                    }
                )
            )
        reading = ModelJoinRefusalReading(
            **base,
            status="owed" if refusals else "answered",
            upstream_failures=len(seen),
            drills_skipped=drills_skipped,
            answered=answered,
            owed=len(refusals),
        )
        return reading, refusals
