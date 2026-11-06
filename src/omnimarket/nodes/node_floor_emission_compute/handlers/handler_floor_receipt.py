# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold emission events into golden and error receipts per piece and per join (OMN-19998, 17a).

Canonical definition-B shape: ``handle(ModelFloorReceiptInput) -> ModelFloorReceipts``. Pure: the clock is the
input's ``now``, nothing is read or written.

A chain is every event of one node and piece under one correlation id. It is golden when a start is followed by a
success, an error chain when a start is followed by a typed failure. A chain that breaks the emission contract is
not counted and is named by a finding: a success or failure with no start, a terminal before its start, a success
and a failure together, a second start, a start with no terminal past the chain deadline.

A join A to B is judged on the events of the two pieces under one correlation id. Golden: A succeeded, B started
after that, B succeeded. Error: A failed and B holds a typed ``upstream_`` failure and never started. B starting
after A failed is a named break, and so is A failing with B silent past the deadline.

Two things are not judged. A join into a piece that does not exist yet (the input lists the pieces that do) cannot be
broken: nothing downstream is there to answer. And a chain run as a drill is not evidence for a join: its fault is
injected on purpose to give the piece its own error receipt, and no downstream piece is expected to react to it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from omnimarket.models.floor_17a import (
    ModelChain,
    ModelChainFinding,
    ModelFloorReceiptInput,
    ModelFloorReceipts,
    ModelJoinDef,
    ModelJoinReceipt,
    ModelNodeEmission,
    ModelPieceReceipt,
)
from omnimarket.models.floor_17a.model_floor_17a import ChainKind

UPSTREAM_PREFIX = "upstream_"


def _age(now: datetime, moment: datetime | None) -> int | None:
    return None if moment is None else max(int((now - moment).total_seconds()), 0)


class _ChainReading:
    """One chain's judgement: the chain, or the findings that kept it out."""

    def __init__(
        self,
        events: list[ModelNodeEmission],
        now: datetime,
        deadline_s: int,
    ) -> None:
        events = sorted(events, key=lambda e: e.emitted_at)
        first = events[0]
        self.piece = first.piece
        self.node = first.node
        self.cid = first.correlation_id
        self.events = events
        self.findings: list[ModelChainFinding] = []
        starts = [e for e in events if e.phase == "start"]
        wins = [e for e in events if e.phase == "success"]
        fails = [e for e in events if e.phase == "failure"]
        subject = f"piece {self.piece} {self.node}"

        def find(code: str, detail: str) -> None:
            self.findings.append(
                ModelChainFinding.model_validate(
                    {
                        "code": code,
                        "subject": subject,
                        "correlation_id": self.cid,
                        "detail": detail,
                    }
                )
            )

        if len(starts) > 1:
            find("duplicate_start", f"{len(starts)} starts under one correlation id")
        if wins and fails:
            find("success_and_failure", "a success and a failure under one id")
        # A typed upstream refusal is a failure that never started: the join's error shape.
        refusal = (
            not starts
            and bool(fails)
            and not wins
            and all((f.failure_code or "").startswith(UPSTREAM_PREFIX) for f in fails)
        )
        if not starts and (wins or fails) and not refusal:
            find(
                "success_without_start" if wins else "failure_without_start",
                "a terminal event with no start",
            )
        if starts:
            for t in (*wins, *fails):
                if t.emitted_at < starts[0].emitted_at:
                    find("terminal_before_start", "a terminal event before the start")
                    break
        terminals = [*wins, *fails]
        if (
            starts
            and not terminals
            and (_age(now, starts[0].emitted_at) or 0) > deadline_s
        ):
            find(
                "start_without_terminal",
                f"no success or typed failure {deadline_s} s after the start",
            )
        self.broken = bool(self.findings)
        last = events[-1]
        self.last = last
        if self.broken:
            self.kind = "broken"
        elif wins and starts:
            self.kind = "golden"
        elif fails:
            self.kind = "error"
        else:
            self.kind = "open"
        self.started_at = starts[0].emitted_at if starts else None
        self.ended_at = terminals[-1].emitted_at if terminals else None
        self.counters: dict[str, int] = {}
        for e in events:
            for k, v in e.counters.items():
                self.counters[k] = max(self.counters.get(k, 0), v)
        self.backend = last.backend
        self.drill = any(e.drill for e in events)

    def chain(self) -> ModelChain:
        return ModelChain.model_validate(
            {
                "piece": self.piece,
                "node": self.node,
                "correlation_id": self.cid,
                "kind": self.kind,
                "started_at": self.started_at,
                "ended_at": self.ended_at,
                "backend": self.backend,
                "drill": self.drill,
                "counters": self.counters,
            }
        )


class _PieceView:
    """Every chain of one piece under one correlation id, as a join sees that piece.

    One cycle runs several nodes of the same piece under one correlation id. A join judges the piece, not a node, so the
    chains are folded together: the piece failed if any chain failed, it is golden only when every chain is, and it started
    when its first chain did. Keying a join on the correlation id and the piece alone let the last chain read replace the
    others, so which node a join judged depended on the order the chains were read in.
    """

    def __init__(self, readings: list[_ChainReading]) -> None:
        self.events = sorted(
            (e for r in readings for e in r.events), key=lambda e: e.emitted_at
        )
        starts = [r.started_at for r in readings if r.started_at is not None]
        ends = [r.ended_at for r in readings if r.ended_at is not None]
        self.started_at = min(starts) if starts else None
        self.ended_at = max(ends) if ends else None
        kinds = {r.kind for r in readings}
        if "broken" in kinds:
            self.kind: ChainKind = "broken"
        elif kinds == {"golden"}:
            self.kind = "golden"
        elif "error" in kinds:
            self.kind = "error"
        else:
            self.kind = "open"
        self.drill = any(r.drill for r in readings)


class HandlerFloorReceipt:
    """Pure fold of emissions into receipts."""

    def handle(self, request: ModelFloorReceiptInput) -> ModelFloorReceipts:
        now = request.now
        grouped: dict[tuple[int, str, str], list[ModelNodeEmission]] = defaultdict(list)
        for event in request.emissions:
            grouped[(event.piece, event.node, event.correlation_id)].append(event)
        readings = [
            _ChainReading(events, now, request.chain_deadline_s)
            for events in grouped.values()
        ]
        findings: list[ModelChainFinding] = [f for r in readings for f in r.findings]
        by_piece: dict[int, list[_ChainReading]] = defaultdict(list)
        grouped_views: dict[tuple[str, int], list[_ChainReading]] = defaultdict(list)
        for reading in readings:
            by_piece[reading.piece].append(reading)
            grouped_views[(reading.cid, reading.piece)].append(reading)
        by_cid = {key: _PieceView(chains) for key, chains in grouped_views.items()}

        pieces: list[ModelPieceReceipt] = []
        for definition in sorted(request.pieces, key=lambda p: p.piece):
            chains = by_piece.get(definition.piece, [])
            golden = [c.ended_at for c in chains if c.kind == "golden" and c.ended_at]
            error = [c.ended_at for c in chains if c.kind == "error" and c.ended_at]
            golden_at = max(golden) if golden else None
            error_at = max(error) if error else None
            seen = [e.emitted_at for c in chains for e in c.events]
            last = max(seen) if seen else None
            g_age, e_age = _age(now, golden_at), _age(now, error_at)
            if last is None:
                freshness = "none"
            elif (
                g_age is not None
                and g_age <= request.freshness_max_age_s
                and e_age is not None
                and e_age <= request.error_max_age_s
            ):
                freshness = "fresh"
            else:
                freshness = "stale"
            counters: dict[str, int] = {}
            for c in chains:
                for k, v in c.counters.items():
                    counters[k] = counters.get(k, 0) + v
            pieces.append(
                ModelPieceReceipt.model_validate(
                    {
                        "piece": definition.piece,
                        "started": golden_at is not None and error_at is not None,
                        "golden_at": golden_at,
                        "error_at": error_at,
                        "golden_backends": tuple(
                            sorted({c.backend for c in chains if c.kind == "golden"})
                        ),
                        "last_emission_at": last,
                        "golden_age_s": g_age,
                        "error_age_s": e_age,
                        "freshness": freshness,
                        "counters": counters,
                    }
                )
            )

        joins: list[ModelJoinReceipt] = []
        for jdef in request.joins:
            receipt, join_findings = self._join(jdef, by_cid, now, request)
            joins.append(receipt)
            findings.extend(join_findings)
        return ModelFloorReceipts.model_validate(
            {
                "computed_at": now,
                "pieces": tuple(pieces),
                "joins": tuple(joins),
                "chains": tuple(r.chain() for r in readings),
                "findings": tuple(findings),
            }
        )

    @staticmethod
    def _join(
        jdef: ModelJoinDef,
        by_cid: dict[tuple[str, int], _PieceView],
        now: datetime,
        request: ModelFloorReceiptInput,
    ) -> tuple[ModelJoinReceipt, list[ModelChainFinding]]:
        found: list[ModelChainFinding] = []
        golden_at: datetime | None = None
        error_at: datetime | None = None
        cids = {c for (c, p) in by_cid if p == jdef.from_piece}
        subject = f"{jdef.id} piece {jdef.from_piece} to {jdef.to_piece}"
        judged = request.judged_to_pieces
        if judged is not None and jdef.to_piece not in judged:
            cids = set()

        def find(code: str, cid: str, detail: str) -> None:
            found.append(
                ModelChainFinding.model_validate(
                    {
                        "code": code,
                        "subject": subject,
                        "correlation_id": cid,
                        "detail": detail,
                    }
                )
            )

        for cid in sorted(cids):
            a = by_cid[(cid, jdef.from_piece)]
            if a.drill:
                continue
            b = by_cid.get((cid, jdef.to_piece))
            a_wins = [e for e in a.events if e.phase == "success"]
            a_fails = [e for e in a.events if e.phase == "failure"]
            if a_wins and b is not None:
                b_start = b.started_at
                if b_start is not None and b_start < a_wins[-1].emitted_at:
                    find(
                        "downstream_started_before_upstream_succeeded",
                        cid,
                        "B started before A succeeded",
                    )
                elif b.kind == "golden" and b.ended_at is not None:
                    golden_at = max(filter(None, (golden_at, b.ended_at)))
            if a_fails:
                if b is None:
                    waited = _age(now, a_fails[-1].emitted_at) or 0
                    if waited > request.chain_deadline_s:
                        find(
                            "downstream_missing_typed_failure",
                            cid,
                            f"A failed and B gave no typed failure in {request.chain_deadline_s} s",
                        )
                elif b.started_at is not None:
                    find(
                        "downstream_started_after_upstream_failed",
                        cid,
                        "B started although A failed",
                    )
                elif b.kind == "error" and b.ended_at is not None:
                    error_at = max(filter(None, (error_at, b.ended_at)))
        broken = bool(found)
        if broken:
            state = "broken"
        elif golden_at and error_at:
            state = "passed"
        elif golden_at:
            state = "golden_only"
        elif error_at:
            state = "error_only"
        else:
            state = "not_run"
        return (
            ModelJoinReceipt.model_validate(
                {
                    "join": jdef.id,
                    "from_piece": jdef.from_piece,
                    "to_piece": jdef.to_piece,
                    "golden_at": golden_at,
                    "error_at": error_at,
                    "state": state,
                }
            ),
            found,
        )
