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

The result is the tick's event, answered whatever happened.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from omnimarket.models.lab_fill import ModelLabFillApprovedRow, ModelLabFillLaunch

from ..models import (
    ModelLabFillLaunchRequest,
    ModelLabFillLaunchResult,
    ModelLabFillTickDispatch,
    ModelLabFillTickRequest,
    ModelLabFillTickResult,
    ModelLabFillTickSkip,
)
from ..models.model_lab_fill_tick import PriorityClass
from ..protocols import (
    LabFillClaimFacts,
    LabFillPortError,
    ProtocolLabFillApprovedWork,
    ProtocolLabFillClock,
    ProtocolLabFillOwnerReader,
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


def lane_name(ticket: str, run_key: str) -> str:
    """``lab-fill-omn123-10170930`` for OMN-123 at run key 2026-10-17T0930Z."""
    digits = re.sub(r"[^0-9]", "", run_key)[4:]
    return f"lab-fill-{ticket.lower().replace('-', '')}-{digits}"


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


class HandlerLabFillTick:
    """Fill a fire's free slots from the approved-work list, deterministically."""

    def __init__(
        self,
        approved: ProtocolLabFillApprovedWork | None = None,
        owners: ProtocolLabFillOwnerReader | None = None,
        launch: HandlerLabFillLaunch | None = None,
        clock: ProtocolLabFillClock | None = None,
    ) -> None:
        from ..protocols.local_lab_fill_adapters import (
            LocalApprovedWork,
            LocalOwnerReader,
        )

        self._approved = approved if approved is not None else LocalApprovedWork()
        self._owners = owners if owners is not None else LocalOwnerReader()
        self._clock = default_clock(clock)
        self._launch = (
            launch if launch is not None else HandlerLabFillLaunch(clock=self._clock)
        )

    def handle(self, request: ModelLabFillTickRequest) -> ModelLabFillTickResult:
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
            else:
                free.append(row)

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
                if reason in _STOP_REASONS or reason.startswith("lane-timeout:"):
                    stopped = reason
            if stopped or not queue:
                break

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


__all__: list[str] = ["HandlerLabFillTick", "lane_name", "priority_class"]
