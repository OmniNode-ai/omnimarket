# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick: one whole lab-fill with no model in the loop (OMN-17427).

The workflow script ran each step through an agent; most steps ran one fixed command
and two re-read each ticket's Linear owner. Here the steps are code, in order:

1. The free slots come from the headroom plan on the request (per host, capped per
   host and per run). No slot: nothing is read.
2. The approved-work list is read once. A row that is malformed or blocked is skipped
   with its reason.
3. Rows are ordered by priority class (mvp-landing, process-fix, other), list order
   breaking ties; a lower class never displaces a running lane, it only orders slots.
4. The claim store is read once for every ticket. A held claim or a live open CLAIM
   owns the row; an unread store owns every row, because unread never means free.
5. Each slot takes the next candidate through ``HandlerLabFillLaunch``, which re-reads
   the ticket's Linear assignee and state directly, writes the brief and detaches the
   remote-lane runner. A candidate the launch refuses for its own reason frees the slot
   for the next candidate; a failure of the runner, the standing rules or the window
   stops the tick and leaves the rest untried rather than spending them.

6. Every lane made and not placed is a refusal with its reason (``refusals=`` on the
   STATUS row reads ``none`` only when every lane made was placed). A ticket the brief
   check refuses ``park_after`` times is parked for ``park_hours`` with its reason, not
   retried every tick.
7. With free slots and nothing placed for ``alarm_after`` ticks running, one diagnose
   lane is dispatched (not again within ``diagnose_cooldown_min``) and the result carries
   the alarm. The streak, the last diagnose and the parked tickets live in the memory file
   at ``state_path``; without one there is no alarm and no parking.

The result is the tick's event, answered whatever happened.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch

from ..models import (
    ModelLabFillLaunchRequest,
    ModelLabFillLaunchResult,
    ModelLabFillTickAlarm,
    ModelLabFillTickDispatch,
    ModelLabFillTickRefusal,
    ModelLabFillTickRequest,
    ModelLabFillTickResult,
    ModelLabFillTickSkip,
    render_refusals,
)
from ..models.model_lab_fill_tick import PriorityClass
from ..protocols import (
    LabFillClaimFacts,
    LabFillPortError,
    ProtocolLabFillApprovedWork,
    ProtocolLabFillClock,
    ProtocolLabFillOwnerReader,
    ProtocolLabFillReceiptReader,
    ProtocolLabFillResultWriter,
)
from .handler_lab_fill_launch import HandlerLabFillLaunch
from .helpers_lab_fill_effect import default_clock

_RANK: dict[PriorityClass, int] = {"mvp-landing": 0, "process-fix": 1, "other": 2}
# Reasons that say the tick, not the candidate, cannot go on.
_STOP_REASONS = frozenset(
    {"deadline", "runner-unavailable", "brief-rules-unreadable", "fences-unreadable"}
)
_TICKET = re.compile(r"^OMN-[0-9]+$")
_ROW_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_PR = re.compile(r"^[A-Za-z0-9_.-]+#[0-9]+$")
_REPO = re.compile(r"^[A-Za-z0-9_.-]+$")


def _digits(run_key: str) -> str:
    return re.sub(r"[^0-9]", "", run_key)[4:]


def lane_name(ticket: str, run_key: str) -> str:
    """``lab-fill-omn123-10170930`` for OMN-123 at run key 2026-10-17T0930Z."""
    return f"lab-fill-{ticket.lower().replace('-', '')}-{_digits(run_key)}"


def diagnose_lane_name(run_key: str) -> str:
    """``lab-fill-diagnose-10170930`` at run key 2026-10-17T0930Z."""
    return f"lab-fill-diagnose-{_digits(run_key)}"


@dataclass
class _Memory:
    """What the tick keeps between fires, as read from and written to the memory file."""

    idle_streak: int = 0
    diagnose_at: float = 0.0
    diagnose_lane: str = ""
    brief_refusals: dict[str, int] = field(default_factory=dict)
    parked: dict[str, dict[str, object]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> _Memory:
        """A missing or malformed file is an empty memory: the streak starts over."""
        if raw is None:
            return cls()
        streak, at, lane = (
            raw.get("idle_streak"),
            raw.get("diagnose_at"),
            raw.get("diagnose_lane"),
        )
        refusals, parked = raw.get("brief_refusals"), raw.get("parked")
        return cls(
            idle_streak=streak if isinstance(streak, int) and streak > 0 else 0,
            diagnose_at=float(at) if isinstance(at, int | float) else 0.0,
            diagnose_lane=lane if isinstance(lane, str) else "",
            brief_refusals={
                str(k): v
                for k, v in (refusals if isinstance(refusals, dict) else {}).items()
                if isinstance(v, int)
            },
            parked={
                str(k): dict(v)
                for k, v in (parked if isinstance(parked, dict) else {}).items()
                if isinstance(v, dict) and isinstance(v.get("at"), int | float)
            },
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "idle_streak": self.idle_streak,
            "diagnose_at": self.diagnose_at,
            "diagnose_lane": self.diagnose_lane,
            "brief_refusals": self.brief_refusals,
            "parked": self.parked,
        }


def priority_class(
    row: Mapping[str, object], mvp_repos: Sequence[str], process_repos: Sequence[str]
) -> PriorityClass:
    """mvp-landing: a PR in an MVP repository; process-fix: a process-fix row or a process repository."""
    repo = str(row.get("repo") or "").split("/")[-1]
    if row.get("pr") and repo in mvp_repos:
        return "mvp-landing"
    if row.get("kind") == "process-fix" or repo in process_repos:
        return "process-fix"
    return "other"


def _slots(request: ModelLabFillTickRequest) -> list[str]:
    slots: list[str] = []
    for host in request.hosts:
        slots += [host.host] * min(host.free, request.max_per_host)
    return slots[: request.max_lanes]


def _epoch(value: str) -> float | None:
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=UTC)
            .timestamp()
        )
    except ValueError:
        return None


def _owner(ticket: str, facts: LabFillClaimFacts | str, now: float) -> tuple[str, str]:
    """``(reason, detail)`` when the ticket is owned or its owner unread; empty when free."""
    if isinstance(facts, str):
        return "owned-unreadable", facts[:200]
    if isinstance(facts.index, str):
        return "owned-unreadable", f"claim-index: {facts.index[:180]}"
    if isinstance(facts.open_claims, str):
        return "owned-unreadable", f"ledger-claims: {facts.open_claims[:180]}"
    record = facts.index.get(ticket)
    if record is not None and record.get("state") == "held":
        return f"owned:{record.get('lane', '')}", "claim-index"
    for claim in facts.open_claims:
        if claim.get("subject") != ticket:
            continue
        when = _epoch(str(claim.get("when", "")))
        if when is None or now - when <= facts.staleness_hours * 3600:
            return f"owned:{claim.get('lane', '')}", "ledger-claims"
    return "", ""


def _brief(
    request: ModelLabFillTickRequest, lane: str, row: Mapping[str, object], marker: str
) -> str:
    return (
        f"# Lane {lane}: {row['ticket']}, approved {row['kind']} row {row['id']}\n\n"
        f"ROUTE: model={request.engine} effort=high\n"
        f"AUTHORITY: operator RULING {request.authority_ruling} "
        f"lane={request.authority_lane}; parent ticket {request.parent_ticket}.\n"
        f"Parent: {request.parent}. Ticket: {row['ticket']}. Repo: {row.get('repo') or 'none'}.\n\n"
        f"{marker}\n\n"
        f"Landing: hand the PR to {request.landing_lane} with needs=land; "
        "never merge or arm it by hand.\n"
    )


def _join(*parts: str) -> str:
    return "; ".join(p for p in parts if p)


def _diagnose_brief(
    request: ModelLabFillTickRequest,
    lane: str,
    result: ModelLabFillTickResult,
    alarm: ModelLabFillTickAlarm,
) -> str:
    reasons = Counter(s.reason.split(":")[0] for s in result.skipped)
    refused = "".join(
        f"- {r.ticket} ({r.lane}@{r.host}) {r.reason}: {' '.join(r.detail.split())[:200]}\n"
        for r in result.refusals
    )
    return (
        f"# Lane {lane}: diagnose lab-fill ticks that left free slots unplaced\n\n"
        f"ROUTE: model={request.engine} effort=high\n"
        f"AUTHORITY: operator RULING {request.authority_ruling} "
        f"lane={request.authority_lane}; parent ticket {request.parent_ticket}.\n"
        f"Parent: {request.parent}. Ticket: {request.parent_ticket}. Repo: none.\n\n"
        f"Lab-fill ticks with free slots placed no lane for {alarm.streak} ticks running.\n"
        f"run_key={request.run_key} fire_id={request.fire_id} "
        f"free_slots={result.free_slots} placed={result.placed} outcome={result.outcome}\n"
        f"refusals={result.refusals_cell}\n"
        f"skipped={','.join(f'{k}:{n}' for k, n in reasons.most_common()) or 'none'}\n"
        f"parked={','.join(result.parked) or 'none'}\n"
        f"{refused}\n"
        "Find why no lane was placed and fix the cause, or write the finding where the "
        "next tick reads it. Do not place the lanes by hand.\n\n"
        f"Landing: hand the PR to {request.landing_lane} with needs=land; "
        "never merge or arm it by hand.\n"
    )


class HandlerLabFillTick:
    """Fill a fire's free slots from the approved-work list, deterministically."""

    def __init__(
        self,
        approved: ProtocolLabFillApprovedWork | None = None,
        owners: ProtocolLabFillOwnerReader | None = None,
        launch: HandlerLabFillLaunch | None = None,
        clock: ProtocolLabFillClock | None = None,
        memory_reader: ProtocolLabFillReceiptReader | None = None,
        memory_writer: ProtocolLabFillResultWriter | None = None,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import (
            LocalApprovedWork,
            LocalOwnerReader,
            LocalReceiptReader,
            LocalResultWriter,
        )

        self._approved = approved if approved is not None else LocalApprovedWork()
        self._owners = owners if owners is not None else LocalOwnerReader()
        self._memory_reader = (
            memory_reader if memory_reader is not None else LocalReceiptReader()
        )
        self._memory_writer = (
            memory_writer if memory_writer is not None else LocalResultWriter()
        )
        self._clock = default_clock(clock)
        self._launch = (
            launch if launch is not None else HandlerLabFillLaunch(clock=self._clock)
        )

    def handle(self, request: ModelLabFillTickRequest) -> ModelLabFillTickResult:
        memory = _Memory.from_mapping(
            self._memory_reader.read(request.state_path) if request.state_path else None
        )
        result = self._fill(request, memory)
        return self._settle(request, memory, result) if request.apply else result

    def _fill(
        self, request: ModelLabFillTickRequest, memory: _Memory
    ) -> ModelLabFillTickResult:
        slots = _slots(request)

        def done(outcome: str, **kw: object) -> ModelLabFillTickResult:
            return ModelLabFillTickResult.model_validate(
                {
                    "run_key": request.run_key,
                    "fire_id": request.fire_id,
                    "outcome": outcome,
                    "free_slots": len(slots),
                    **kw,
                }
            )

        if not slots:
            return done("no-headroom", detail="no host has a free slot")
        try:
            rows = list(self._approved.list_rows(request.approved_work_path))
        except LabFillPortError as exc:
            return done("approved-work-unreadable", detail=str(exc)[:300])

        skipped: list[ModelLabFillTickSkip] = []
        candidates: list[Mapping[str, object]] = []
        for row in rows:
            ticket, row_id = str(row.get("ticket", "")), str(row.get("id", ""))
            if not (
                _TICKET.match(ticket) and _ROW_ID.match(row_id) and row.get("kind")
            ):
                skipped.append(
                    ModelLabFillTickSkip(
                        ticket=ticket, row_id=row_id, reason="invalid-row"
                    )
                )
            elif row.get("blocked_until"):
                skipped.append(
                    ModelLabFillTickSkip(
                        ticket=ticket,
                        row_id=row_id,
                        reason="blocked",
                        detail=str(row["blocked_until"])[:200],
                    )
                )
            else:
                candidates.append(row)
        candidates.sort(
            key=lambda r: _RANK[
                priority_class(r, request.mvp_repos, request.process_repos)
            ]
        )

        facts: LabFillClaimFacts | str
        try:
            facts = self._owners.claims(
                request.ledger_path, sorted({str(r["ticket"]) for r in candidates})
            )
        except LabFillPortError as exc:
            facts = f"{type(exc).__name__}:{exc}"
        now = self._clock.now_epoch_s()
        free: list[Mapping[str, object]] = []
        for row in candidates:
            reason, detail = _owner(str(row["ticket"]), facts, now)
            if reason:
                skipped.append(
                    ModelLabFillTickSkip(
                        ticket=str(row["ticket"]),
                        row_id=str(row["id"]),
                        reason=reason,
                        detail=detail,
                    )
                )
            elif (
                held := self._parked(request, memory, str(row["ticket"]), now)
            ) is not None:
                skipped.append(
                    ModelLabFillTickSkip(
                        ticket=str(row["ticket"]),
                        row_id=str(row["id"]),
                        reason=f"parked:{held['reason']}",
                        detail=str(held["detail"]),
                    )
                )
            else:
                free.append(row)

        refusals: list[ModelLabFillTickRefusal] = []
        parked_now: list[str] = []
        dispatched: list[ModelLabFillTickDispatch] = []
        stopped = ""
        queue = deque(free)
        for i, host in enumerate(slots):
            while queue and not stopped:
                row = queue.popleft()
                ticket, row_id = str(row["ticket"]), str(row["id"])
                lane = lane_name(ticket, request.run_key)
                priority = priority_class(row, request.mvp_repos, request.process_repos)
                if not request.apply:
                    dispatched.append(
                        ModelLabFillTickDispatch(
                            lane=lane,
                            ticket=ticket,
                            row_id=row_id,
                            host=host,
                            priority=priority,
                        )
                    )
                    break
                launched = self._launch_one(
                    request, row, lane, host, lanes_left=len(slots) - i
                )
                if launched.detached:
                    memory.brief_refusals.pop(ticket, None)
                    dispatched.append(
                        ModelLabFillTickDispatch(
                            lane=lane,
                            ticket=ticket,
                            row_id=row_id,
                            host=host,
                            priority=priority,
                            receipt=launched.receipt,
                        )
                    )
                    break
                reason = launched.skipped or "runner-refused"
                skipped.append(
                    ModelLabFillTickSkip(
                        ticket=ticket,
                        row_id=row_id,
                        reason=reason,
                        detail=launched.detail,
                    )
                )
                refusals.append(
                    ModelLabFillTickRefusal(
                        lane=lane,
                        ticket=ticket,
                        row_id=row_id,
                        host=host,
                        reason=reason,
                        detail=launched.detail,
                    )
                )
                if reason == "brief-check-refused" and self._park(
                    request, memory, ticket, launched.detail, self._clock.now_epoch_s()
                ):
                    parked_now.append(ticket)
                if reason in _STOP_REASONS or reason.startswith("lane-timeout:"):
                    stopped = reason
            if stopped or not queue:
                break

        if stopped:
            refusals.extend(
                ModelLabFillTickRefusal(
                    lane=lane_name(str(r["ticket"]), request.run_key),
                    ticket=str(r["ticket"]),
                    row_id=str(r["id"]),
                    host=slots[0],
                    reason=f"untried:{stopped}",
                )
                for r in queue
            )
        if not request.apply:
            outcome = "planned"
        else:
            outcome = "dispatched" if dispatched else "nothing-dispatched"
        return done(
            outcome,
            dispatched=tuple(dispatched),
            skipped=tuple(skipped),
            untried=tuple(str(r["ticket"]) for r in queue),
            stopped=stopped,
            refusals=tuple(refusals),
            refusals_cell=render_refusals(tuple(refusals)),
            parked=tuple(parked_now),
            placed=0 if outcome == "planned" else len(dispatched),
        )

    @staticmethod
    def _parked(
        request: ModelLabFillTickRequest, memory: _Memory, ticket: str, now: float
    ) -> dict[str, object] | None:
        """The park entry that still holds the ticket; an expired one is dropped with its count."""
        held = memory.parked.get(ticket)
        if held is None:
            return None
        if now - float(str(held["at"])) < request.park_hours * 3600:
            return {"reason": held.get("reason", ""), "detail": held.get("detail", "")}
        del memory.parked[ticket]
        memory.brief_refusals.pop(ticket, None)
        return None

    @staticmethod
    def _park(
        request: ModelLabFillTickRequest,
        memory: _Memory,
        ticket: str,
        detail: str,
        now: float,
    ) -> bool:
        """Count a brief-check refusal; park the ticket at the ``park_after``-th."""
        count = memory.brief_refusals.get(ticket, 0) + 1
        memory.brief_refusals[ticket] = count
        if count < request.park_after:
            return False
        memory.parked[ticket] = {
            "reason": "brief-check-refused",
            "detail": f"{count} refusals: {detail}"[:300],
            "at": now,
        }
        return True

    def _settle(
        self,
        request: ModelLabFillTickRequest,
        memory: _Memory,
        result: ModelLabFillTickResult,
    ) -> ModelLabFillTickResult:
        """Count the idle streak, send the diagnose lane when it is due, and keep the memory."""
        if not request.state_path:
            return result.model_copy(
                update={
                    "detail": _join(
                        result.detail,
                        "memory off (no state_path): no idle alarm, no parking",
                    )
                }
            )
        now = self._clock.now_epoch_s()
        idle = result.free_slots > 0 and result.placed == 0
        memory.idle_streak = memory.idle_streak + 1 if idle else 0
        alarm = ModelLabFillTickAlarm(
            streak=memory.idle_streak, free_slots=result.free_slots
        )
        if idle and memory.idle_streak >= request.alarm_after:
            alarm = self._diagnose(request, memory, result, alarm, now)
        memory.parked = {
            t: p
            for t, p in memory.parked.items()
            if now - float(str(p["at"])) < request.park_hours * 3600
        }
        detail = result.detail
        try:
            self._memory_writer.write(request.state_path, memory.to_mapping())
        except LabFillPortError as exc:
            detail = _join(detail, f"memory-unwritable: {str(exc)[:200]}")
        return result.model_copy(update={"alarm": alarm, "detail": detail})

    def _diagnose(
        self,
        request: ModelLabFillTickRequest,
        memory: _Memory,
        result: ModelLabFillTickResult,
        alarm: ModelLabFillTickAlarm,
        now: float,
    ) -> ModelLabFillTickAlarm:
        """One diagnose lane per cooldown: dispatched now, or deduplicated against the last."""
        if memory.diagnose_at and now - memory.diagnose_at < (
            request.diagnose_cooldown_min * 60
        ):
            return alarm.model_copy(
                update={
                    "diagnose": "deduplicated",
                    "diagnose_lane": memory.diagnose_lane,
                }
            )
        lane = diagnose_lane_name(request.run_key)
        launched = self._launch.handle(
            ModelLabFillLaunchRequest(
                launch=ModelLabFillLaunch(
                    lane=lane,
                    ticket=request.parent_ticket,
                    engine=request.engine,
                    host=_slots(request)[0],
                    parent=request.parent,
                    timeout_min=request.timeout_min,
                ),
                kind="diagnose",
                brief=_diagnose_brief(request, lane, result, alarm),
                brief_dir=request.brief_dir,
                ledger_path=request.ledger_path,
                approved_work_path=request.approved_work_path,
                operator_id=request.operator_id,
                window_end_epoch_s=request.window_end_epoch_s,
            )
        )
        if not launched.detached:
            reason = launched.skipped or "runner-refused"
            return alarm.model_copy(
                update={
                    "diagnose": "refused",
                    "diagnose_lane": lane,
                    "diagnose_detail": f"{reason}: {launched.detail}"[:300],
                }
            )
        memory.diagnose_at, memory.diagnose_lane = now, lane
        return alarm.model_copy(
            update={
                "diagnose": "dispatched",
                "diagnose_lane": lane,
                "diagnose_receipt": launched.receipt,
            }
        )

    def _launch_one(
        self,
        request: ModelLabFillTickRequest,
        row: Mapping[str, object],
        lane: str,
        host: str,
        *,
        lanes_left: int,
    ) -> ModelLabFillLaunchResult:
        ticket, row_id, kind = str(row["ticket"]), str(row["id"]), str(row["kind"])
        marker = f"APPROVED-ROW {row_id} (unresolved)"
        repo = str(row.get("repo") or "").split("/")[-1]
        pr = str(row.get("pr") or "")
        launch = ModelLabFillLaunch(
            lane=lane,
            ticket=ticket,
            engine=request.engine,
            host=host,
            parent=request.parent,
            timeout_min=request.timeout_min,
            repo=repo if _REPO.match(repo) else None,
            pr=pr if _PR.match(pr) else None,
            approved_row=ModelLabFillApprovedRow(
                id=row_id, kind=kind, ticket=ticket, marker=marker
            ),
        )
        return self._launch.handle(
            ModelLabFillLaunchRequest(
                launch=launch,
                kind=kind,
                brief=_brief(request, lane, row, marker),
                brief_dir=request.brief_dir,
                ledger_path=request.ledger_path,
                approved_work_path=request.approved_work_path,
                operator_id=request.operator_id,
                window_end_epoch_s=request.window_end_epoch_s,
                lanes_left=max(1, lanes_left),
            )
        )


__all__: list[str] = [
    "HandlerLabFillTick",
    "diagnose_lane_name",
    "lane_name",
    "priority_class",
]
