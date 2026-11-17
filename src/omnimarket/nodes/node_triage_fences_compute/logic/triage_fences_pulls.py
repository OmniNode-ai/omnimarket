# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Open-pull-request fences of the triage fence derivation (OMN-18040, OMN-20682).

Moved from the deleted ``triage_fences`` module without a change of rule.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from ..models import ModelTriageFencesOpenPull, ModelTriageFencesPullFence
from .triage_fences_grammar import (
    BRANCH_TICKET_RE,
    TICKET_TEAM,
    iso,
    parse_ts,
    sort_tickets,
    ticket_sort_key,
    tickets_in,
)


def pr_fences_from_pulls(
    pulls: Iterable[ModelTriageFencesOpenPull],
    *,
    window_hours: float,
    now: datetime,
) -> tuple[ModelTriageFencesPullFence, ...]:
    """Tickets with an open PR anywhere in the registry, matched on the OMN- id
    in the PR title or its head branch.

    OMN-18040 AC1: an open PR is fenced on being OPEN, not on being RECENT.
    ``window_hours`` used to be a membership gate -- a PR quiet for longer than
    the window (an untouched, still-open PR is exactly the live-claimed scope
    this fence exists to protect) silently fell out of the fence list with no
    error and no truncation signal. It is now reporting-only: every field
    still carries ``updated_at``/``age_hours``/``within_window`` so a reader
    can see how stale a fenced PR is, but staleness never removes it from
    ``fenced_tickets``.
    """
    collected: list[ModelTriageFencesPullFence] = []
    seen: set[tuple[str, str, int]] = set()
    for pull in pulls:
        # AC1: membership is "this PR is open", full stop. `updated_at`
        # missing/unparseable no longer drops the PR -- it just cannot
        # report an age.
        updated = parse_ts(pull.updated_at)
        age_hours = (
            round((now - updated).total_seconds() / 3600.0, 2)
            if updated is not None
            else None
        )
        within_window = age_hours is not None and age_hours <= window_hours
        in_title = tickets_in(pull.title)
        in_branch = frozenset(
            f"{TICKET_TEAM}-{n}" for n in BRANCH_TICKET_RE.findall(pull.head_ref_name)
        )
        for ticket in sort_tickets(in_title | in_branch):
            key = (ticket, pull.repo, pull.number or 0)
            if key in seen:
                continue
            seen.add(key)
            collected.append(
                ModelTriageFencesPullFence(
                    ticket=ticket,
                    repo=pull.repo,
                    number=pull.number,
                    title=pull.title,
                    branch=pull.head_ref_name,
                    url=pull.url,
                    updated_at=iso(updated) if updated is not None else None,
                    age_hours=age_hours,
                    within_window=within_window,
                    matched_on="title" if ticket in in_title else "branch",
                )
            )
    collected.sort(key=lambda f: (ticket_sort_key(f.ticket), f.repo, f.number or 0))
    return tuple(collected)
