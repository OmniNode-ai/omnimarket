# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Enumerate: read the lab fill's list sources, each under its own budget floor (OMN-23250).

The old read (the frozen lab-fill script) shared one budget across plan-stalled,
ruling-pending, lane-resend and source-tickets: a slow plans-today read (14.8 s, of which
13.8 s is the whole-ledger re-read) left source-tickets without time, and that stage then
discarded the lists the others had read, so all three sources were reported unreadable
although read (stoppage:lab-source-unreadable).

Here a stage may spend only what is left after the floors of the stages behind it, a stage
that is cut marks itself stale and nothing else, the lists survive a cut ticket stage, and
plans-today is served from a saved copy while it is younger than the request's bound.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping

from ..models.model_lab_fill_sources import (
    SOURCE_KINDS,
    STAGES,
    TICKETS_STAGE,
    ModelLabFillSourceCandidate,
    ModelLabFillSourceDrop,
    ModelLabFillSourceRead,
    ModelLabFillSourcesRequest,
    ModelLabFillSourcesResult,
    SourceState,
)
from ..protocols import (
    LabFillListed,
    LabFillPortError,
    LabFillSourceCutError,
    LabFillTicketNode,
    ProtocolLabFillClock,
    ProtocolLabFillPlansCopy,
    ProtocolLabFillSourceReaders,
)
from .helpers_lab_fill_effect import default_clock

_PLANS = "plan-stalled"


def _detail(exc: Exception) -> str:
    return " ".join(str(exc).split())[:100]


class HandlerLabFillSources:
    """Read each list source, then resolve the listed tickets, inside per-stage budgets."""

    def __init__(
        self,
        plans: ProtocolLabFillPlansCopy,
        readers: ProtocolLabFillSourceReaders,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        self._plans = plans
        self._readers = readers
        self._clock = default_clock(clock)

    def handle(self, request: ModelLabFillSourcesRequest) -> ModelLabFillSourcesResult:
        start = self._clock.now_epoch_s()
        stages = [s for s in STAGES if s == TICKETS_STAGE or s in request.sources]
        reads: list[ModelLabFillSourceRead] = []
        lists: dict[str, LabFillListed] = {}
        for index, stage in enumerate(stages[:-1]):
            allowed = self._allowed(request, stage, start, len(stages) - index - 1)
            began = self._clock.now_epoch_s()
            read, listed = self._read_source(request, stage, allowed)
            reads.append(
                read.model_copy(update={"elapsed_s": self._clock.now_epoch_s() - began})
            )
            if listed is not None:
                lists[stage] = listed
        wanted = sorted({t for listed in lists.values() for t, _ in listed.found})
        allowed = self._allowed(request, TICKETS_STAGE, start, 0)
        began = self._clock.now_epoch_s()
        nodes: Mapping[str, LabFillTicketNode] = {}
        state: SourceState = "empty"
        detail = ""
        if wanted:
            try:
                nodes = self._readers.read_tickets(wanted, allowed)
                state = "ok"
            except LabFillSourceCutError as exc:
                state, detail = "stale", _detail(exc)
            except LabFillPortError as exc:
                state, detail = "unreadable", _detail(exc)
        reads.append(
            ModelLabFillSourceRead(
                stage=TICKETS_STAGE,
                state=state,
                tickets=tuple(wanted),
                detail=detail,
                budget_s=allowed,
                elapsed_s=self._clock.now_epoch_s() - began,
            )
        )
        candidates, dropped = self._candidates(lists, nodes, resolved=state == "ok")
        return ModelLabFillSourcesResult(
            reads=tuple(reads),
            candidates=tuple(candidates),
            dropped=tuple(dropped),
            stale=tuple(r.stage for r in reads if r.state == "stale"),
            unreadable=tuple(r.stage for r in reads if r.state == "unreadable"),
            elapsed_s=self._clock.now_epoch_s() - start,
        )

    def _allowed(
        self, request: ModelLabFillSourcesRequest, stage: str, start: float, behind: int
    ) -> float:
        """What the stage may spend: what is left less the floors of the stages behind it, at least its own floor."""
        remaining = request.budget_s - (self._clock.now_epoch_s() - start)
        spend = max(remaining - request.stage_floor_s * behind, request.stage_floor_s)
        cap = request.stage_cap_s.get(stage)
        return min(spend, cap) if cap is not None else spend

    def _read_source(
        self, request: ModelLabFillSourcesRequest, stage: str, allowed: float
    ) -> tuple[ModelLabFillSourceRead, LabFillListed | None]:
        copy_age: float | None = None
        try:
            if stage == _PLANS:
                listed, copy_age = self._read_plans(request, allowed)
            elif stage == "ruling-pending":
                listed = self._readers.read_rulings(allowed)
            else:
                listed = self._readers.read_resends(allowed)
        except LabFillSourceCutError as exc:
            served = self._served_copy(request, stage)
            if served is not None:
                return self._read(
                    stage, "stale", served[0], allowed, _detail(exc), served[1]
                )
            return self._read(stage, "stale", None, allowed, _detail(exc), None)
        except LabFillPortError as exc:
            return self._read(stage, "unreadable", None, allowed, _detail(exc), None)
        return self._read(
            stage, "ok" if listed.found else "empty", listed, allowed, "", copy_age
        )

    def _read_plans(
        self, request: ModelLabFillSourcesRequest, allowed: float
    ) -> tuple[LabFillListed, float | None]:
        age = self._plans.age_min()
        if age is not None and age <= request.plans_copy_max_age_min:
            return self._plans.load(), age
        return self._plans.rebuild(allowed), None

    def _served_copy(
        self, request: ModelLabFillSourcesRequest, stage: str
    ) -> tuple[LabFillListed, float] | None:
        if stage != _PLANS:
            return None
        age = self._plans.age_min()
        if age is None or age > request.plans_copy_stale_max_age_min:
            return None
        return self._plans.load(), age

    @staticmethod
    def _read(
        stage: str,
        state: SourceState,
        listed: LabFillListed | None,
        allowed: float,
        detail: str,
        copy_age: float | None,
    ) -> tuple[ModelLabFillSourceRead, LabFillListed | None]:
        tickets = tuple(dict.fromkeys(t for t, _ in listed.found)) if listed else ()
        read = ModelLabFillSourceRead(
            stage=stage,
            state=state,
            tickets=tickets,
            detail=detail,
            budget_s=allowed,
            elapsed_s=0.0,
            from_copy_age_min=copy_age,
        )
        return read, listed

    @staticmethod
    def _candidates(
        lists: Mapping[str, LabFillListed],
        nodes: Mapping[str, LabFillTicketNode],
        *,
        resolved: bool,
    ) -> tuple[list[ModelLabFillSourceCandidate], list[ModelLabFillSourceDrop]]:
        drops: Counter[tuple[str, str]] = Counter()
        out: list[ModelLabFillSourceCandidate] = []
        for kind in SOURCE_KINDS:
            listed = lists.get(kind)
            if listed is None:
                continue
            for reason, count in listed.dropped:
                drops[(kind, reason)] += count
            for ticket, evidence in dict(listed.found).items():
                if not resolved:
                    out.append(
                        ModelLabFillSourceCandidate(
                            kind=kind, ticket=ticket, evidence=evidence, resolved=False
                        )
                    )
                    continue
                node = nodes.get(ticket)
                if node is None:
                    drops[(kind, "ticket-unreadable")] += 1
                elif node.state_type in ("completed", "canceled"):
                    drops[(kind, "done")] += 1
                elif node.has_children:
                    drops[(kind, "has-children")] += 1
                elif node.open_prs:
                    drops[(kind, "open-pr")] += 1
                else:
                    out.append(
                        ModelLabFillSourceCandidate(
                            kind=kind,
                            ticket=ticket,
                            evidence=evidence,
                            resolved=True,
                            title=node.title[:120],
                            state_type=node.state_type,
                        )
                    )
        return out, [
            ModelLabFillSourceDrop(kind=k, reason=r, count=c)
            for (k, r), c in sorted(drops.items())
        ]
