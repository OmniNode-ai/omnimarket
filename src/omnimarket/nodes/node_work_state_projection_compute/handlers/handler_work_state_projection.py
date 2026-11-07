# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-state projection: every work item, from the sources, with its state and its start criterion's reading.

Pure and deterministic: the clock reading, the rules, the scope and every source row come in on the request; nothing is
read from a file, the floor record or the floor page. Definition-B:

    handle(ModelWorkStateRequest) -> ModelWorkStateResult

Items are tracker tickets, plan file tasks, pull requests, their red required checks and lanes. Each gets one state, in
this order of precedence:

    done         the ticket's state type is a done type, the plan task is marked done or its ticket is done, the pull
                 request is merged or closed, the lane closed with a done outcome
    in_progress  a live lane is attributed to it (an open pull request carries its ticket; a green open pull request is
                 with landing)
    stopped      the newest lane attributed to it stopped (a TERMINAL whose outcome is not a done outcome, an ended
                 receipt with no TERMINAL, or no closing row past the live window), with what it left
    blocked      its start criterion reads false or unread, naming the blocking items
    available    its start criterion reads true

Lanes are attributed to items only through the cells of their CLAIM rows that the overlay names (ticket, owns, cause,
key); no owner is typed anywhere. An item outside the goal's scope declaration is projected too and marked paused.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_work_state_projection_compute.models import (
    EnumCriterionReading,
    EnumLaneState,
    EnumScopeStatus,
    EnumWorkItemKind,
    EnumWorkItemState,
    ModelCommitmentSource,
    ModelCriterionPart,
    ModelLaneReceiptSource,
    ModelPullRequestSource,
    ModelWorkItem,
    ModelWorkScope,
    ModelWorkStateRequest,
    ModelWorkStateResult,
    ModelWorkStateRules,
)

CLAIM = "CLAIM"
TERMINAL = "TERMINAL"
COMMITMENT_VERIFIED = "verified"
COMMITMENT_REGRESSED = "regressed"
OUTCOME_CELL = "outcome"
READING_PREFIX = "reading:"
_PR_REF = re.compile(r"^(?:[\w.-]+/)?(?P<repo>[\w.-]+)#(?P<number>\d+)$")


def _commitment_reason(c: ModelCommitmentSource) -> str:
    """Why an unfinished commitment is work: its check has not passed yet, or it passed and then failed."""
    if c.status == COMMITMENT_REGRESSED:
        return f"regressed: check failed (exit {c.last_exit}) after it passed; owed by {c.owner}"
    return f"open: check has not passed; owed by {c.owner}"


def pr_key(repo: str, number: int) -> str:
    return f"pr:{repo}#{number}"


def check_key(repo: str, number: int, check: str) -> str:
    return f"check:{repo}#{number}:{check}"


def normalize_ref(ref: str, rules: ModelWorkStateRules) -> str:
    """A criterion reference as an item key: `ZEP-1` -> `ticket:ZEP-1`, `owner/repo#9` -> `pr:repo#9`."""
    ref = ref.strip()
    if re.match(rules.ticket_id_pattern, ref):
        return f"ticket:{ref}"
    match = _PR_REF.match(ref)
    if match:
        return pr_key(match["repo"], int(match["number"]))
    if ref.startswith("pr:"):
        inner = _PR_REF.match(ref[3:])
        if inner:
            return pr_key(inner["repo"], int(inner["number"]))
    return ref


def _cells_of(row: ModelLedgerEvidenceRow) -> list[tuple[str, str]]:
    """Every key=value cell of the row, repeats kept (a CLAIM may carry several owns cells)."""
    out: list[tuple[str, str]] = []
    for part in row.raw.split(" | ")[2:]:
        key, sep, value = part.partition("=")
        key = key.strip()
        if sep and key and " " not in key and value.strip():
            out.append((key, value.strip()))
    return out


@dataclass
class _Lane:
    name: str
    claims: list[ModelLedgerEvidenceRow] = field(default_factory=list)
    after: list[ModelLedgerEvidenceRow] = field(default_factory=list)
    aliases: set[str] = field(default_factory=set)
    state: EnumLaneState = EnumLaneState.LIVE
    reason: str = ""
    left: str | None = None
    first: datetime | None = None


def _lane_aliases(
    claim: ModelLedgerEvidenceRow, rules: ModelWorkStateRules
) -> set[str]:
    aliases: set[str] = set()
    for key, value in _cells_of(claim):
        prefix = rules.attribution_cells.get(key)
        if prefix is None:
            continue
        if prefix == "pr":
            aliases.add(
                normalize_ref(value, rules) if _PR_REF.match(value) else f"pr:{value}"
            )
        elif prefix == "key":
            aliases.add(value)
        else:
            aliases.add(f"{prefix}:{value}")
    return aliases


def _read_lanes(
    rows: tuple[ModelLedgerEvidenceRow, ...],
    receipts: tuple[ModelLaneReceiptSource, ...],
    rules: ModelWorkStateRules,
    now: datetime,
) -> dict[str, _Lane]:
    by_lane: dict[str, list[ModelLedgerEvidenceRow]] = defaultdict(list)
    for r in rows:
        if r.lane:
            by_lane[r.lane].append(r)
    receipt_of = {r.lane: r for r in receipts}
    lanes: dict[str, _Lane] = {}
    for name, lane_rows in by_lane.items():
        lane_rows.sort(key=lambda r: (r.stamp, r.raw))
        claims = [r for r in lane_rows if r.row_type == CLAIM]
        if not claims:
            continue
        lane = _Lane(name=name, claims=claims, first=claims[0].stamp)
        for c in claims:
            lane.aliases |= _lane_aliases(c, rules)
        # The run is the one the newest CLAIM opened, or the first one when later CLAIMs are child claims of a run that
        # has not closed yet: a run ends at its TERMINAL.
        run_start = claims[0].stamp
        for r in lane_rows:
            if r.row_type == TERMINAL and r.stamp >= run_start:
                later = [c for c in claims if c.stamp > r.stamp]
                if later:
                    run_start = later[0].stamp
        lane.after = [r for r in lane_rows if r.stamp >= run_start]
        terminal = next(
            (r for r in reversed(lane.after) if r.row_type == TERMINAL), None
        )
        left_parts: list[str] = []
        for r in lane.after:
            for key, value in _cells_of(r):
                if key in rules.left_cells:
                    part = f"{key}={value}"
                    if part not in left_parts:
                        left_parts.append(part)
        receipt = receipt_of.get(name)
        if terminal is not None:
            outcome = terminal.cells.get(OUTCOME_CELL, "")
            if outcome in rules.terminal_done_outcomes:
                lane.state = EnumLaneState.DONE
                lane.reason = f"closed outcome={outcome}"
            else:
                lane.state = EnumLaneState.STOPPED
                lane.reason = f"closed outcome={outcome or 'none'}"
                lane.left = "; ".join([f"outcome={outcome or 'none'}", *left_parts])
        elif receipt is not None and receipt.status in rules.receipt_ended_statuses:
            lane.state = EnumLaneState.STOPPED
            lane.reason = f"receipt status={receipt.status} exit={receipt.exit_code} with no closing row"
            lane.left = "; ".join([f"receipt={receipt.status}", *left_parts])
        else:
            newest = lane.after[-1].stamp
            if now - newest > timedelta(hours=rules.lane_live_hours):
                lane.state = EnumLaneState.STOPPED
                hours = (now - newest).total_seconds() / 3600
                lane.reason = f"no closing row; newest row {hours:.1f}h ago"
                lane.left = "; ".join(["outcome=none", *left_parts])
            else:
                lane.state = EnumLaneState.LIVE
                lane.reason = "claimed, not closed"
        lanes[name] = lane
    return lanes


@dataclass
class _Draft:
    key: str
    kind: EnumWorkItemKind
    title: str
    ticket: str | None
    aliases: set[str]
    done: bool
    done_reason: str = ""
    requires: tuple[str, ...] = ()
    scope: EnumScopeStatus = EnumScopeStatus.PAUSED
    pr: ModelPullRequestSource | None = None
    check: str | None = None
    commitment: ModelCommitmentSource | None = None


def _ticket_in_scope(
    ticket: str | None, scope: ModelWorkScope, parents: dict[str, str | None]
) -> bool:
    seen: set[str] = set()
    while ticket and ticket not in seen:
        if ticket in scope.paused_tickets:
            return False
        if ticket in scope.in_scope_tickets:
            return True
        seen.add(ticket)
        ticket = parents.get(ticket)
    return False


class HandlerWorkStateProjection:
    """Project every work item from the request's sources."""

    def handle(self, request: ModelWorkStateRequest) -> ModelWorkStateResult:
        rules = request.rules
        scope = request.scope
        parents = {t.ticket_id: t.parent for t in request.tickets}
        projects = {t.ticket_id: t.project for t in request.tickets}
        done_tickets = {
            t.ticket_id
            for t in request.tickets
            if t.state_type in rules.ticket_done_state_types
        }

        def ticket_scope(ticket: str | None) -> bool:
            if ticket is None:
                return False
            if ticket in scope.paused_tickets:
                return False
            if _ticket_in_scope(ticket, scope, parents):
                return True
            return (
                bool(projects.get(ticket))
                and projects.get(ticket) in scope.in_scope_projects
            )

        drafts: dict[str, _Draft] = {}
        for t in request.tickets:
            key = f"ticket:{t.ticket_id}"
            drafts[key] = _Draft(
                key=key,
                kind=EnumWorkItemKind.TICKET,
                title=t.title,
                ticket=t.ticket_id,
                aliases={key},
                done=t.ticket_id in done_tickets,
                done_reason=f"ticket state type {t.state_type}",
                requires=tuple(normalize_ref(r, rules) for r in t.requires),
                scope=EnumScopeStatus.IN_SCOPE
                if ticket_scope(t.ticket_id)
                else EnumScopeStatus.PAUSED,
            )
        for p in request.plan_tasks:
            key = f"plan:{p.plan}#{p.task_id}"
            ticket_done = p.ticket is not None and p.ticket in done_tickets
            in_scope = p.plan in scope.in_scope_plans or ticket_scope(p.ticket)
            drafts[key] = _Draft(
                key=key,
                kind=EnumWorkItemKind.PLAN_TASK,
                title=p.title,
                ticket=p.ticket,
                aliases={key} | ({f"ticket:{p.ticket}"} if p.ticket else set()),
                done=p.done or ticket_done,
                done_reason="plan marks the task done"
                if p.done
                else f"ticket {p.ticket} done",
                requires=tuple(normalize_ref(r, rules) for r in p.requires),
                scope=EnumScopeStatus.IN_SCOPE if in_scope else EnumScopeStatus.PAUSED,
            )
        open_pr_of_ticket: dict[str, list[str]] = defaultdict(list)
        for pr in request.pull_requests:
            key = pr_key(pr.repo, pr.number)
            is_open = pr.state.upper() == "OPEN"
            short = f"{pr.repo}#{pr.number}"
            in_scope = short not in scope.paused_prs and (
                short in scope.in_scope_prs
                or pr.repo in scope.in_scope_repos
                or ticket_scope(pr.ticket)
            )
            scope_status = (
                EnumScopeStatus.IN_SCOPE if in_scope else EnumScopeStatus.PAUSED
            )
            if is_open and pr.ticket:
                open_pr_of_ticket[pr.ticket].append(key)
            drafts[key] = _Draft(
                key=key,
                kind=EnumWorkItemKind.PULL_REQUEST,
                title=pr.title,
                ticket=pr.ticket,
                aliases={key},
                done=not is_open,
                done_reason=f"pull request {pr.state.lower()}",
                scope=scope_status,
                pr=pr,
            )
            if not is_open:
                continue
            for check in pr.red_required_checks:
                ckey = check_key(pr.repo, pr.number, check)
                aliases = {ckey, key}
                cause = pr.cause_keys.get(check)
                if cause:
                    aliases.add(f"cause:{cause}")
                drafts[ckey] = _Draft(
                    key=ckey,
                    kind=EnumWorkItemKind.RED_CHECK,
                    title=check,
                    ticket=pr.ticket,
                    aliases=aliases,
                    done=False,
                    scope=scope_status,
                    pr=pr,
                    check=check,
                )

        for c in request.commitments:
            key = f"commitment:{c.commitment_id}"
            drafts[key] = _Draft(
                key=key,
                kind=EnumWorkItemKind.COMMITMENT,
                title=c.ask,
                ticket=c.ticket,
                aliases={key},
                done=c.status == COMMITMENT_VERIFIED,
                done_reason=f"check passes (exit {c.last_exit})",
                requires=tuple(normalize_ref(r, rules) for r in c.requires),
                # An operator's ask is in scope by being asked; only a ticket the goal pauses pauses it.
                scope=EnumScopeStatus.PAUSED
                if c.ticket in scope.paused_tickets
                else EnumScopeStatus.IN_SCOPE,
                commitment=c,
            )

        lanes = _read_lanes(request.ledger_rows, request.receipts, rules, request.now)
        lanes_of: dict[str, list[_Lane]] = defaultdict(list)
        for draft in drafts.values():
            for lane in lanes.values():
                if draft.aliases & lane.aliases:
                    lanes_of[draft.key].append(lane)
        readings = {r.key: r.value for r in request.readings}

        def item_state(key: str) -> tuple[bool, str] | None:
            """Whether an item key reads done, or None when no source names it."""
            d = drafts.get(key)
            if d is None:
                return None
            return d.done, d.done_reason

        def read_criterion(
            draft: _Draft,
        ) -> tuple[EnumCriterionReading, list[ModelCriterionPart], list[str]]:
            parts: list[ModelCriterionPart] = []
            blocking: list[str] = []
            for ref in draft.requires:
                if ref.startswith(READING_PREFIX):
                    name, _, want = ref[len(READING_PREFIX) :].partition("=")
                    got = readings.get(name)
                    if got is None:
                        parts.append(
                            ModelCriterionPart(
                                ref=ref,
                                reading=EnumCriterionReading.UNREAD,
                                detail="no reading",
                            )
                        )
                        blocking.append(f"{READING_PREFIX}{name}")
                    elif got == want:
                        parts.append(
                            ModelCriterionPart(
                                ref=ref, reading=EnumCriterionReading.TRUE, detail=got
                            )
                        )
                    else:
                        parts.append(
                            ModelCriterionPart(
                                ref=ref, reading=EnumCriterionReading.FALSE, detail=got
                            )
                        )
                        blocking.append(f"{READING_PREFIX}{name}")
                    continue
                state = item_state(ref)
                if state is None:
                    parts.append(
                        ModelCriterionPart(
                            ref=ref,
                            reading=EnumCriterionReading.UNREAD,
                            detail="no source names it",
                        )
                    )
                    blocking.append(ref)
                elif state[0]:
                    parts.append(
                        ModelCriterionPart(
                            ref=ref, reading=EnumCriterionReading.TRUE, detail=state[1]
                        )
                    )
                else:
                    parts.append(
                        ModelCriterionPart(
                            ref=ref,
                            reading=EnumCriterionReading.FALSE,
                            detail="not done",
                        )
                    )
                    blocking.append(ref)
            readings_seen = {p.reading for p in parts}
            if EnumCriterionReading.UNREAD in readings_seen:
                overall = EnumCriterionReading.UNREAD
            elif EnumCriterionReading.FALSE in readings_seen:
                overall = EnumCriterionReading.FALSE
            else:
                overall = EnumCriterionReading.TRUE
            return overall, parts, blocking

        items: list[ModelWorkItem] = []
        for key in sorted(drafts):
            d = drafts[key]
            attributed = sorted(
                lanes_of.get(key, []), key=lambda ln: (ln.first, ln.name)
            )
            live = [ln for ln in attributed if ln.state is EnumLaneState.LIVE]
            newest = attributed[-1] if attributed else None
            criterion, parts, blocking = read_criterion(d)
            left: str | None = None
            live_lane: str | None = None
            if d.done:
                state, reason = EnumWorkItemState.DONE, d.done_reason
            elif live:
                live_lane = live[-1].name
                state, reason = EnumWorkItemState.IN_PROGRESS, f"lane {live_lane} live"
            elif newest is not None and newest.state is EnumLaneState.STOPPED:
                state, reason, left = (
                    EnumWorkItemState.STOPPED,
                    f"lane {newest.name} {newest.reason}",
                    newest.left,
                )
            elif d.kind is EnumWorkItemKind.PULL_REQUEST and d.pr is not None:
                reds = sorted(
                    check_key(d.pr.repo, d.pr.number, c)
                    for c in d.pr.red_required_checks
                )
                decision = d.pr.controller_decision or "no decision read"
                if reds:
                    state, reason = (
                        EnumWorkItemState.BLOCKED,
                        f"red required checks; controller {decision}",
                    )
                    blocking = reds
                    criterion = EnumCriterionReading.FALSE
                else:
                    state, reason = (
                        EnumWorkItemState.IN_PROGRESS,
                        f"with landing; controller {decision}",
                    )
            elif (
                d.ticket
                and d.kind is not EnumWorkItemKind.RED_CHECK
                and open_pr_of_ticket.get(d.ticket)
            ):
                prs = ", ".join(open_pr_of_ticket[d.ticket])
                state, reason = (
                    EnumWorkItemState.IN_PROGRESS,
                    f"open pull request {prs}",
                )
            elif criterion is not EnumCriterionReading.TRUE:
                state = EnumWorkItemState.BLOCKED
                reason = f"start criterion reads {criterion.value}: waits for {', '.join(blocking)}"
            else:
                state = EnumWorkItemState.AVAILABLE
                reason = (
                    "start criterion reads true" if d.requires else "no prerequisite"
                )
                if d.commitment is not None:
                    reason = _commitment_reason(d.commitment)
            items.append(
                ModelWorkItem(
                    key=key,
                    kind=d.kind,
                    title=d.title,
                    ticket=d.ticket,
                    state=state,
                    reason=reason,
                    scope=d.scope,
                    criterion=criterion,
                    criterion_parts=tuple(parts),
                    blocking=tuple(blocking)
                    if state is EnumWorkItemState.BLOCKED
                    else (),
                    lanes=tuple(ln.name for ln in attributed),
                    live_lane=live_lane,
                    left=left,
                    owner=d.commitment.owner if d.commitment else None,
                )
            )

        scope_of = {i.key: i.scope for i in items}
        for name in sorted(lanes):
            lane = lanes[name]
            owned = sorted(k for k, ls in lanes_of.items() if lane in ls)
            lane_scope = (
                EnumScopeStatus.IN_SCOPE
                if any(scope_of.get(k) is EnumScopeStatus.IN_SCOPE for k in owned)
                else EnumScopeStatus.PAUSED
            )
            state = {
                EnumLaneState.LIVE: EnumWorkItemState.IN_PROGRESS,
                EnumLaneState.STOPPED: EnumWorkItemState.STOPPED,
                EnumLaneState.DONE: EnumWorkItemState.DONE,
            }[lane.state]
            items.append(
                ModelWorkItem(
                    key=f"lane:{name}",
                    kind=EnumWorkItemKind.LANE,
                    title=", ".join(owned),
                    state=state,
                    reason=lane.reason,
                    scope=lane_scope,
                    criterion=EnumCriterionReading.TRUE,
                    lanes=(name,),
                    live_lane=name if lane.state is EnumLaneState.LIVE else None,
                    left=lane.left,
                )
            )

        counts: dict[str, int] = {s.value: 0 for s in EnumWorkItemState}
        for i in items:
            counts[i.state.value] += 1
        return ModelWorkStateResult(
            as_of=request.now,
            items=tuple(items),
            unread=request.unread,
            counts=counts,
        )


__all__ = ["HandlerWorkStateProjection", "check_key", "normalize_ref", "pr_key"]
