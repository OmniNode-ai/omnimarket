# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The work-in-progress admission rule (RULING 2026-10-30T00:03:58Z).

New work is not started while the count of started-but-unlanded work is at the limit.
Fix-forward work for a red, a repeat break or a landing blocker is never refused,
because it is what makes work land; creating a ticket is never refused by count,
because a ticket is a record. Every number comes in as an argument (the contract's
``config.wip_admission`` block, the deployment overlay's policy); none is a literal here.

``build_wip_read`` turns the facts a caller holds (the PR watcher state, the live
CLAIMs, the tickets' states) into the read; ``count_started_unlanded`` counts it;
``decide_wip_admission`` answers one request.
"""

from __future__ import annotations

import math
import re
from collections.abc import Collection, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.models.wip_admission import (
    EnumWipFixClass,
    EnumWipItemKind,
    EnumWipOutcome,
    EnumWipReason,
    EnumWipWorkKind,
    ModelWipAdmissionConfig,
    ModelWipAdmissionDecision,
    ModelWipAdmissionRequest,
    ModelWipClaim,
    ModelWipCounts,
    ModelWipItem,
    ModelWipLimit,
    ModelWipRead,
)


def derive_limit(
    merges_per_hour: float, lead_time_hours: float, config: ModelWipAdmissionConfig
) -> int:
    """Little's law: ``max(limit_floor, ceil(merges per hour x target lead time))``."""
    return max(config.limit_floor, math.ceil(merges_per_hour * lead_time_hours))


def limit_is_derived(limit: ModelWipLimit, config: ModelWipAdmissionConfig) -> bool:
    """A stated limit must be the one its stated measurement gives."""
    return limit.limit == derive_limit(
        limit.merges_per_hour, limit.lead_time_hours, config
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _unreadable(reason: str) -> ModelWipRead:
    return ModelWipRead(readable=False, unreadable_reason=reason)


def build_wip_read(
    *,
    pr_state: Mapping[str, Any] | None,
    claims: Collection[ModelWipClaim],
    ticket_states: Mapping[str, str] | None,
    config: ModelWipAdmissionConfig,
) -> ModelWipRead:
    """The read from the PR watcher state, live CLAIMs and ticket states.

    An absent or malformed watcher state is an unreadable read, never zero. A claim
    counts only when its ticket's state is one of ``config.started_states``; a ticket
    whose state is not known is left out. ``ticket_states`` of ``None`` means the caller
    holds no ticket states at all, and a live CLAIM then stands for started.
    """
    if pr_state is None:
        return _unreadable("watcher state absent")
    prs = pr_state.get("prs")
    read_at = _parse_time(pr_state.get("last_tick"))
    if not isinstance(prs, Mapping) or read_at is None:
        return _unreadable("watcher state malformed")
    ticket_re = re.compile(config.ticket_pattern)
    items: list[ModelWipItem] = []
    for record in prs.values():
        facts = record.get("facts") if isinstance(record, Mapping) else None
        if not isinstance(facts, Mapping) or not isinstance(facts.get("repo"), str):
            return _unreadable("watcher record malformed")
        if facts.get("state") != "OPEN" or facts.get("merged_at"):
            continue
        cls = str(record.get("cls") or "")
        if cls in config.excluded_classes:
            continue
        repo = facts["repo"].split("/")[-1]
        text = " ".join(str(facts.get(k) or "") for k in ("title", "head_ref"))
        items.append(
            ModelWipItem(
                kind=EnumWipItemKind.PULL_REQUEST,
                repo=repo,
                ref=f"{repo}#{facts.get('number')}",
                tickets=tuple(sorted(set(ticket_re.findall(text)))),
                draft=bool(facts.get("draft")),
                red=cls in config.red_classes,
            )
        )
    started = set(config.started_states)
    for claim in claims:
        if ticket_states is None or ticket_states.get(claim.ticket) in started:
            items.append(
                ModelWipItem(
                    kind=EnumWipItemKind.TICKET_CLAIM,
                    repo=claim.repo.split("/")[-1],
                    ref=claim.ticket,
                    tickets=(claim.ticket,),
                )
            )
    return ModelWipRead(readable=True, read_at=read_at, items=tuple(items))


def count_started_unlanded(
    read: ModelWipRead, *, own_ticket: str = ""
) -> ModelWipCounts:
    """Open non-draft PRs plus started tickets with a live CLAIM, once each.

    A ticket a counted pull request cites is that pull request's, not a second item.
    ``own_ticket`` (the caller's) is left out so a re-dispatch never blocks itself.
    """
    counted_prs = [
        i
        for i in read.items
        if i.kind is EnumWipItemKind.PULL_REQUEST
        and not i.draft
        and not (own_ticket and own_ticket in i.tickets)
    ]
    carried = {t for pr in counted_prs for t in pr.tickets}
    claims = [
        i
        for i in read.items
        if i.kind is EnumWipItemKind.TICKET_CLAIM
        and i.ref != own_ticket
        and i.ref not in carried
    ]
    by_repo: dict[str, int] = {}
    for item in (*counted_prs, *claims):
        if item.repo:
            by_repo[item.repo] = by_repo.get(item.repo, 0) + 1
    return ModelWipCounts(
        by_repo=by_repo,
        fleet=len(counted_prs) + len(claims),
        pull_requests=len(counted_prs),
        ticket_claims=len(claims),
    )


def _read_problem(
    read: ModelWipRead, now: datetime, config: ModelWipAdmissionConfig
) -> EnumWipReason | None:
    if not read.readable or read.read_at is None:
        return EnumWipReason.READ_MISSING
    if now - read.read_at > timedelta(minutes=config.reading_max_age_minutes):
        return EnumWipReason.READ_STALE
    return None


def _fix_verified(request: ModelWipAdmissionRequest) -> bool:
    fix = request.fix
    if fix is None or fix.fix_class not in request.policy.fix_forward_classes:
        return False
    if fix.fix_class is EnumWipFixClass.REPEAT_BREAK:
        return fix.repeat_count >= request.config.repeat_break_min
    targets = {
        i.ref: i for i in request.read.items if i.kind is EnumWipItemKind.PULL_REQUEST
    }
    target = targets.get(fix.target)
    if target is None:
        return False
    return target.red if fix.fix_class is EnumWipFixClass.RED else True


def _line(
    verb: str,
    request: ModelWipAdmissionRequest,
    reason: EnumWipReason,
    detail: str = "",
) -> str:
    scope = request.repo or "fleet"
    return f"{verb} wip:{scope} kind={request.kind.value} reason={reason.value}{detail}"


def decide_wip_admission(
    request: ModelWipAdmissionRequest,
) -> ModelWipAdmissionDecision:
    """Admit or refuse one request to start work."""

    def decision(
        outcome: EnumWipOutcome,
        reason: EnumWipReason,
        counts: ModelWipCounts | None = None,
        repo_limit: int | None = None,
    ) -> ModelWipAdmissionDecision:
        repo_count = counts.by_repo.get(request.repo, 0) if counts else None
        fleet_count = counts.fleet if counts else None
        fleet_limit = request.policy.fleet.limit
        detail = (
            f" repo={repo_count}/{repo_limit} fleet={fleet_count}/{fleet_limit}"
            if counts
            else ""
        )
        verb = "ADMIT" if outcome is EnumWipOutcome.ADMIT else "REFUSE"
        return ModelWipAdmissionDecision(
            outcome=outcome,
            reason=reason,
            kind=request.kind,
            repo=request.repo,
            repo_count=repo_count,
            repo_limit=repo_limit,
            fleet_count=fleet_count,
            fleet_limit=fleet_limit if counts else None,
            line=_line(verb, request, reason, detail),
        )

    admit = EnumWipOutcome.ADMIT
    refuse = EnumWipOutcome.REFUSE
    if request.kind is EnumWipWorkKind.TICKET_CREATE:
        return decision(admit, EnumWipReason.TICKET_RECORD)
    problem = _read_problem(request.read, request.now, request.config)
    if request.kind is EnumWipWorkKind.FIX_FORWARD:
        # A fix is never refused; one whose evidence the read cannot confirm still
        # goes through when the read itself is down, because refusing it is what
        # keeps the red unlanded.
        if problem is not None:
            return decision(admit, EnumWipReason.FIX_FORWARD_READ_UNUSABLE)
        if _fix_verified(request):
            counts = count_started_unlanded(request.read, own_ticket=request.ticket)
            return decision(admit, EnumWipReason.FIX_FORWARD, counts)
    if not request.policy.enforcing:
        return decision(admit, EnumWipReason.NOT_ENFORCING)
    if problem is not None:
        return decision(refuse, problem)
    limit = request.policy.limits.get(request.repo) if request.repo else None
    if request.repo and limit is None:
        return decision(refuse, EnumWipReason.LIMIT_MISSING)
    counts = count_started_unlanded(request.read, own_ticket=request.ticket)
    repo_limit = limit.limit if limit is not None else None
    if limit is not None and counts.by_repo.get(request.repo, 0) >= limit.limit:
        return decision(refuse, EnumWipReason.AT_REPO_LIMIT, counts, repo_limit)
    if counts.fleet >= request.policy.fleet.limit:
        return decision(refuse, EnumWipReason.AT_FLEET_LIMIT, counts, repo_limit)
    return decision(admit, EnumWipReason.UNDER_LIMIT, counts, repo_limit)
