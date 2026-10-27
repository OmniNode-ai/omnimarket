# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure ticket-backlog selection for idle lab slots (OMN-20864).

Operator ruling 2026-10-20T10:45:21Z places new lab-fill logic in omnimarket;
the plugin only calls it. Open, unassigned current/next sprint tickets join the
existing M4-carrier and delegation sources when they have unchecked criteria.
Every issue passes the same ordered gates and yields an item or a counted drop.
The rule reads no file, network or clock; all facts arrive in the request.
The live gate re-applies the closed and other-owner exclusions to Linear read just
before the send, because the snapshot is read once a day.

Kept exclusions, each with its ruling: closed tickets; tickets assigned to someone other
than the operator; tickets a live CLAIM or an open PR already carries; repositories the
caller puts in ``out_of_scope`` (the merge sweep's scope setting, parked and retired by
RULING 2026-10-18T05:25:04Z, plus the repository RULING 2026-10-17T20:42:15Z keeps out
of M4, which the caller names because this public repository does not); dashboard work
(RULING 2026-10-01T10:53:36Z, OMN-19969).
"""

from __future__ import annotations

import re
from datetime import date, timedelta

from omnimarket.models.lab_fill import (
    EnumLabFillLiveSkip,
    EnumLabFillTicketDrop,
    EnumLabFillTicketSource,
    ModelLabFillLiveFacts,
    ModelLabFillLivePlan,
    ModelLabFillLiveVerdict,
    ModelLabFillTicketDrop,
    ModelLabFillTicketFacts,
    ModelLabFillTicketItem,
    ModelLabFillTicketPlan,
)

# RULING 2026-10-01T10:53:36Z: dashboard stay-out (OMN-19969).
DASHBOARD_TICKETS = frozenset({"OMN-19969"})
DASHBOARD_CRITERIA = frozenset({"C31", "C32"})
SPRINT_RE = re.compile(
    r"^Sprint\s+(?P<start>\d{4}-\d{2}-\d{2})\s*(?:→|->|to)\s*"
    r"(?P<end>\d{4}-\d{2}-\d{2})(?=\s|:|$)",
    re.IGNORECASE,
)
NEXT_SPRINT_DAYS = 7
_CRITERION_RE = re.compile(
    r"^[ \t]*[-*][ \t]+\[ \][ \t]+(?P<bold>\*\*)?"
    r"(?P<label>[A-Za-z]+-?[A-Za-z0-9]*)(?(bold)\*\*)[ \t]*:",
    re.MULTILINE,
)
_SOURCE_ORDER = tuple(EnumLabFillTicketSource)
_SPRINT_SOURCES = frozenset(
    {EnumLabFillTicketSource.SPRINT_CURRENT, EnumLabFillTicketSource.SPRINT_NEXT}
)


def gate_live_tickets(facts: ModelLabFillLiveFacts) -> ModelLabFillLivePlan:
    """Recheck each unique requested ticket with ordered, fail-closed live gates."""
    live = {ticket.id: ticket for ticket in facts.live}
    operators = set(facts.operators) | {"me"}
    verdicts: list[ModelLabFillLiveVerdict] = []
    for ticket_id in dict.fromkeys(facts.tickets):
        ticket = live.get(ticket_id)
        reason: EnumLabFillLiveSkip | None = None
        detail = ""
        if facts.error:
            reason = EnumLabFillLiveSkip.UNREADABLE
            detail = facts.error[:200]
        elif ticket is None:
            reason = EnumLabFillLiveSkip.NOT_FOUND
        elif ticket.state_type.lower() in {"completed", "canceled", "cancelled"} or (
            ticket.state_name.lower() in {"done", "canceled", "cancelled", "duplicate"}
        ):
            reason = EnumLabFillLiveSkip.CLOSED
            detail = f"state {ticket.state_type or ticket.state_name}"
        elif (
            (ticket.assignee_id or ticket.assignee_name)
            and ticket.assignee_id not in operators
            and ticket.assignee_name not in operators
        ):
            reason = EnumLabFillLiveSkip.OTHER_OWNER
            detail = f"assignee {ticket.assignee_id or ticket.assignee_name}"
        verdicts.append(
            ModelLabFillLiveVerdict(
                id=ticket_id, send=reason is None, reason=reason, detail=detail
            )
        )
    return ModelLabFillLivePlan(verdicts=tuple(verdicts))


def sprint_window(project: str, today: date) -> EnumLabFillTicketSource | None:
    """Classify a parseable current sprint or one starting within seven days."""
    match = SPRINT_RE.match(project)
    if match is None:
        return None
    try:
        start = date.fromisoformat(match["start"])
        end = date.fromisoformat(match["end"])
    except ValueError:
        return None
    if end < start:
        return None
    if start <= today <= end:
        return EnumLabFillTicketSource.SPRINT_CURRENT
    if today < start <= today + timedelta(days=NEXT_SPRINT_DAYS):
        return EnumLabFillTicketSource.SPRINT_NEXT
    return None


def parse_criteria(description: str) -> tuple[str, ...]:
    """Unchecked labelled checkbox criteria, deduplicated in description order."""
    labels = (
        match["label"]
        for match in _CRITERION_RE.finditer(description)
        if any(char.isdigit() for char in match["label"])
    )
    return tuple(dict.fromkeys(labels))


def plan_ticket_backlog(facts: ModelLabFillTicketFacts) -> ModelLabFillTicketPlan:
    """Apply first-match gates to every issue, then stably rank admitted sources."""
    today = date.fromisoformat(facts.today)
    excluded = set(facts.out_of_scope)
    excluded_patterns = {
        repo: re.compile(rf"(?<![\w-]){re.escape(repo)}(?![\w-])", re.IGNORECASE)
        for repo in sorted(excluded)
    }
    open_pr = set(facts.open_pr)
    claimed = set(facts.claimed)
    items: list[ModelLabFillTicketItem] = []
    drops: list[ModelLabFillTicketDrop] = []
    for issue in facts.issues:
        if issue.status_type.lower() in {"completed", "canceled", "cancelled"} or (
            issue.status.lower() in {"done", "canceled", "cancelled", "duplicate"}
        ):
            drops.append(
                ModelLabFillTicketDrop(id=issue.id, reason=EnumLabFillTicketDrop.CLOSED)
            )
            continue
        if issue.assignee and issue.assignee not in {facts.operator, "me"}:
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id,
                    reason=EnumLabFillTicketDrop.OTHER_OWNER,
                    detail=issue.assignee,
                )
            )
            continue

        carriers = tuple(
            sorted(label for label, ids in facts.m4_carriers.items() if issue.id in ids)
        )
        source: EnumLabFillTicketSource | None
        if carriers:
            source = EnumLabFillTicketSource.M4_CARRIER
        elif (
            issue.pillar == "delegation"
            or any("delegation" in label.lower() for label in issue.labels)
            or "delegation" in issue.project.lower()
        ):
            source = EnumLabFillTicketSource.DELEGATION
        else:
            source = sprint_window(issue.project, today)
        if source is None:
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id,
                    reason=EnumLabFillTicketDrop.NOT_PILLAR,
                    detail=issue.project or "no project",
                )
            )
            continue

        text = " ".join((issue.title, *issue.labels, issue.project))
        excluded_repo = next(
            (
                repo
                for repo, pattern in excluded_patterns.items()
                if pattern.search(text)
            ),
            None,
        )
        if issue.repo in excluded or excluded_repo is not None:
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id,
                    reason=EnumLabFillTicketDrop.OUT_OF_SCOPE,
                    detail=issue.repo
                    if issue.repo in excluded
                    else excluded_repo or "",
                )
            )
            continue
        if (
            issue.id in DASHBOARD_TICKETS
            or DASHBOARD_CRITERIA.intersection(carriers)
            or issue.repo == "omnidash"
            or "dashboard" in text.lower()
            or "omnidash" in text.lower()
        ):
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id, reason=EnumLabFillTicketDrop.DASHBOARD
                )
            )
            continue
        if issue.id in open_pr:
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id, reason=EnumLabFillTicketDrop.OPEN_PR
                )
            )
            continue
        if issue.id in claimed:
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id, reason=EnumLabFillTicketDrop.CLAIMED
                )
            )
            continue
        criteria = parse_criteria(issue.description)
        if source in _SPRINT_SOURCES and not criteria:
            drops.append(
                ModelLabFillTicketDrop(
                    id=issue.id, reason=EnumLabFillTicketDrop.NO_CRITERIA
                )
            )
            continue
        items.append(
            ModelLabFillTicketItem(
                id=issue.id,
                title=issue.title,
                repo=issue.repo,
                status=issue.status,
                source=source,
                carriers=carriers,
                project=issue.project,
                criteria=criteria,
            )
        )
    items.sort(key=lambda item: _SOURCE_ORDER.index(item.source))
    return ModelLabFillTicketPlan(items=tuple(items), drops=tuple(drops))


def drop_line(plan: ModelLabFillTicketPlan) -> str:
    """Stable ticket and drop counts for the lab-fill diagnostic line."""
    counts = ", ".join(
        f"{reason}={count}" for reason, count in plan.drop_counts().items()
    )
    return f"tickets={len(plan.items)} dropped: {counts or 'none'}"
