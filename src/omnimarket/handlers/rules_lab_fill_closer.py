# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rule that keeps a lab-fill lane off a ticket whose work is already built (OMN-23376): pure, shared by the dispatch
plan and the send gate so the two cannot disagree.

A ticket goes to the closer, and is not dispatched, when a merged change names it, or when a lane reported it already built and
its falsifiers passed. A lane of a kind that works on one named pull request, and the fill's own diagnose lane, is never stopped
by a merge. The facts come in as arguments; nothing here reads a file, a clone, the network or the clock.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

from omnimarket.models.lab_fill.model_lab_fill_closer import (
    EnumLabFillCloserReason,
    ModelLabFillBuiltReport,
    ModelLabFillCloserRoute,
)

CLOSER_NODE = "node_dod_closeout_sweep_compute"
# A lane of these kinds acts on one named pull request or is the fill's own, so a merge naming its ticket does not stop it.
CLOSER_EXEMPT_KINDS: tuple[str, ...] = ("pr-red", "pr-stalled", "pr-land", "diagnose")
# The fill's DoD-verify lane exists only for a ticket whose implementation merged, so it is always the closer's work now.
DOD_VERIFY_KIND = "dod-verify"
# The TERMINAL outcomes a lane writes when it found the ticket's work already built.
BUILT_OUTCOMES = frozenset({"skipped-already-built", "already-built"})
_TICKET = re.compile(r"OMN-[0-9]+")
_NUMBERED_SQUASH = re.compile(r"\(#([0-9]+)\)\s*$")


def built_reports_from_ledger(
    lines: Iterable[str],
) -> tuple[ModelLabFillBuiltReport, ...]:
    """The already-built TERMINALs among the ledger rows, oldest first; falsifiers pass only on a ``falsifiers=pass`` cell."""
    reports: list[ModelLabFillBuiltReport] = []
    for line in lines:
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 3 or cells[1] != "TERMINAL":
            continue
        values = dict(cell.split("=", 1) for cell in cells[2:] if "=" in cell)
        if values.get("outcome", "").lower() not in BUILT_OUTCOMES:
            continue
        tickets = sorted(set(_TICKET.findall(values.get("ticket", ""))))
        lane = values.get("lane", "")
        text = values.get("evidence") or (
            cells[-1] if cells[-1] and "=" not in cells[-1] else ""
        )
        reports.extend(
            ModelLabFillBuiltReport(
                ticket=ticket,
                lane=lane,
                reported_at=cells[0],
                falsifiers_passed=values.get("falsifiers", "").lower() == "pass",
                evidence=text[:400],
            )
            for ticket in tickets
        )
    return tuple(sorted(reports, key=lambda r: (r.reported_at, r.ticket)))


def merges_from_clone_log(
    repo: str, log: str, tickets: Iterable[str], *, evidence_prefix: str
) -> dict[str, tuple[str, ...]]:
    """The merges one canonical clone's ``git log --format=%H<TAB>%s`` names for each ticket asked about.

    A squash merge titled ``... (#123)`` reads ``repo#123``; one with no number reads ``repo@<short sha>``. A change-control
    evidence commit is not implementation and never counts."""
    wanted = set(tickets)
    found: dict[str, set[str]] = {}
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if not sha or subject.lower().startswith(evidence_prefix.lower()):
            continue
        number = _NUMBERED_SQUASH.search(subject)
        name = f"{repo}#{number[1]}" if number else f"{repo}@{sha[:9]}"
        for ticket in set(_TICKET.findall(subject)) & wanted:
            found.setdefault(ticket, set()).add(name)
    return {ticket: tuple(sorted(names)) for ticket, names in found.items()}


def closer_route(
    ticket: str,
    kind: str,
    *,
    merged_prs: Iterable[str] = (),
    built: Iterable[ModelLabFillBuiltReport] = (),
    exempt_kinds: Iterable[str] = CLOSER_EXEMPT_KINDS,
    closer: str = CLOSER_NODE,
) -> ModelLabFillCloserRoute | None:
    """The route to the closer for a ticket, or None when a lane may be dispatched for it.

    The newest passing already-built report of the ticket supplies the evidence; a report whose falsifiers did not pass
    stops nothing, because the closer closes on evidence and a failed falsifier says the work is not done."""
    if kind in set(exempt_kinds):
        return None
    prs = tuple(sorted(set(merged_prs)))
    passing = [r for r in built if r.ticket == ticket and r.falsifiers_passed]
    report = max(passing, key=lambda r: r.reported_at, default=None)
    if prs or kind == DOD_VERIFY_KIND:
        reason = EnumLabFillCloserReason.IMPLEMENTATION_MERGED
    elif report is not None:
        reason = EnumLabFillCloserReason.ALREADY_BUILT
    else:
        return None
    return ModelLabFillCloserRoute(
        ticket=ticket,
        reason=reason,
        prs=prs,
        lane=report.lane if report else "",
        evidence=report.evidence if report else "",
        closer=closer,
    )


def merged_by_ticket(
    sources: Iterable[Mapping[str, Iterable[str]]],
) -> dict[str, tuple[str, ...]]:
    """The union of several merged-work readings (the PR watcher, each canonical clone), ticket to sorted pull requests."""
    union: dict[str, set[str]] = {}
    for source in sources:
        for ticket, prs in source.items():
            union.setdefault(ticket, set()).update(prs)
    return {ticket: tuple(sorted(prs)) for ticket, prs in union.items()}
