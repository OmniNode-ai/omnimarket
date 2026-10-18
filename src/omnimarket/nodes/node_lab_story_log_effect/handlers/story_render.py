# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Collect a week's rows into themes and render the story log as markdown.

Every piece of free text from a row passes through the sanitizer before it is placed in a
sentence, and the finished document is checked once more by the caller.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from omnimarket.nodes.node_lab_story_log_effect.handlers.hygiene import Sanitizer
from omnimarket.nodes.node_lab_story_log_effect.handlers.ledger_rows import (
    LedgerRow,
    MergedPr,
    cost_minutes,
)
from omnimarket.nodes.node_lab_story_log_effect.handlers.story_themes import (
    THEME_ORDER,
    theme_of,
)

_MAX_INCIDENTS_PER_THEME = 6
_MAX_FIXES_PER_THEME = 8
_MAX_PRS_PER_RULING = 4
_SNIPPET = 220
INCIDENT_KINDS = ("FRICTION", "CORRECTION", "HOLD")


@dataclass
class Incident:
    key: str
    kind: str
    first: datetime
    last: datetime
    count: int = 0
    minutes: float = 0.0
    priced: int = 0
    example: str = ""
    tickets: set[str] = field(default_factory=set)


def stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%MZ")


def _snippet(text: str, limit: int = _SNIPPET) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:-") + "…"


def _count(n: int, singular: str, plural: str | None = None) -> str:
    return f"{n:,} {singular if n == 1 else (plural or singular + 's')}"


def _minutes(value: float) -> str:
    return f"{value:,.0f}" if abs(value - round(value)) < 0.05 else f"{value:,.1f}"


def collect_incidents(
    rows: list[LedgerRow], clean: Sanitizer
) -> dict[str, list[Incident]]:
    """Incidents grouped by theme; one incident per (kind, class or guard) across the week."""
    grouped: dict[tuple[str, str, str], Incident] = {}
    for row in rows:
        if row.kind not in INCIDENT_KINDS:
            continue
        label = (
            row.get("class") or row.get("guard") or row.get("scope") or row.kind.lower()
        )
        label = label.split(":", 1)[0][:60]
        theme = theme_of(
            label, row.get("guard"), row.get("reason"), row.get("detail"), row.text
        )
        key = (theme, row.kind, label)
        incident = grouped.get(key)
        if incident is None:
            incident = grouped[key] = Incident(
                key=clean.sanitize(label),
                kind=row.kind,
                first=row.stamp,
                last=row.stamp,
            )
        incident.count += 1
        incident.last = max(incident.last, row.stamp)
        incident.first = min(incident.first, row.stamp)
        minutes = cost_minutes(row.get("cost"))
        if minutes is not None:
            incident.minutes += minutes
            incident.priced += 1
        if not incident.example:
            incident.example = clean.sanitize(_snippet(row.get("detail") or row.text))
        for ticket_key in ("existing", "ticket"):
            if row.get(ticket_key).startswith("OMN-"):
                incident.tickets.add(row.get(ticket_key))
    by_theme: dict[str, list[Incident]] = defaultdict(list)
    for key, incident in grouped.items():
        by_theme[key[0]].append(incident)
    for items in by_theme.values():
        items.sort(key=lambda i: (-i.minutes, -i.count, i.key))
    return by_theme


def incident_sentence(incident: Incident) -> str:
    noun = {"FRICTION": "friction row", "CORRECTION": "correction", "HOLD": "hold"}[
        incident.kind
    ]
    span = (
        f"on {stamp(incident.first)}"
        if stamp(incident.first) == stamp(incident.last)
        else f"between {stamp(incident.first)} and {stamp(incident.last)}"
    )
    sentence = f"`{incident.key}`: {_count(incident.count, noun)} {span}"
    if incident.priced:
        sentence += (
            f", with a stated cost of {_minutes(incident.minutes)} minutes "
            f"across {_count(incident.priced, 'row')} that named one"
        )
    sentence += "."
    if incident.example:
        sentence += f" The first row read: “{incident.example}”"
    return sentence


def render_log(
    *,
    week_label: str,
    start: datetime,
    end: datetime,
    rows: list[LedgerRow],
    prs: list[MergedPr],
    clean: Sanitizer,
) -> str:
    kinds: dict[str, int] = defaultdict(int)
    outcomes: dict[str, int] = defaultdict(int)
    for row in rows:
        kinds[row.kind] += 1
        if row.kind == "TERMINAL":
            outcomes[row.get("outcome")] += 1
    incidents = collect_incidents(rows, clean)
    friction_minutes = sum(
        m
        for r in rows
        if r.kind == "FRICTION" and (m := cost_minutes(r.get("cost"))) is not None
    )
    priced = sum(
        1
        for r in rows
        if r.kind == "FRICTION" and cost_minutes(r.get("cost")) is not None
    )
    last = rows[-1].stamp if rows else None
    out: list[str] = [
        "---",
        f"title: Lab story log, ISO week {week_label}",
        f"week: {week_label}",
        f"range: {start.date().isoformat()} to {(end - timedelta(days=1)).date().isoformat()}",
        f"last_ledger_row: {stamp(last) if last else 'none'}",
        "generator: omnimarket node_lab_story_log_effect",
        "---",
        "",
        f"# Lab story log, {week_label}",
        "",
        (
            f"This log covers the ledger rows stamped from {stamp(start)} up to, but not including, "
            f"{stamp(end)}. The newest row it read is stamped {stamp(last) if last else 'never'}, "
            "so a week still in progress reads as partial. Private repository names, addresses, "
            "host names and people's names are replaced by roles, and the operator's own words are "
            "left out: the operator writes the article, this log is the facts and the structure."
        ),
        "",
        "## The week in numbers",
        "",
        (
            f"The ledger recorded {_count(len(rows), 'row')} this week, of which "
            f"{_count(kinds['RULING'], 'was a ruling', 'were rulings')}, "
            f"{_count(kinds['FRICTION'], 'was a friction row', 'were friction rows')} and "
            f"{_count(kinds['CORRECTION'], 'was a correction', 'were corrections')}. "
            f"{_count(len(prs), 'pull request')} merged and were adopted into the lab. "
            f"{_count(outcomes['blocked'], 'lane')} ended blocked and "
            f"{_count(outcomes['failed'], 'lane')} ended failed, against "
            f"{_count(outcomes['handed-off'] + outcomes['done'], 'lane')} that ended handed off or done."
        ),
        "",
        (
            f"Friction rows that named a cost add up to {_minutes(friction_minutes)} minutes "
            f"across {_count(priced, 'row')} out of {kinds['FRICTION']:,}; the rest named no figure."
        ),
        "",
        "## Rulings and the pull requests behind them",
        "",
    ]
    rulings = [r for r in rows if r.kind == "RULING"]
    if not rulings:
        out += ["The operator recorded no ruling this week.", ""]
    for ruling in rulings:
        out += _ruling_block(ruling, prs, clean)
    out += ["## Themes", ""]
    fixes_by_theme: dict[str, list[MergedPr]] = defaultdict(list)
    for pr in prs:
        fixes_by_theme[theme_of(pr.title)].append(pr)
    for theme in THEME_ORDER:
        items = incidents.get(theme, [])
        fixes = fixes_by_theme.get(theme, [])
        if not items and not fixes:
            continue
        out += [f"### {theme}", ""]
        if items:
            total = sum(i.count for i in items)
            out += [
                f"{_count(len(items), 'kind of incident', 'kinds of incident')} fell under this theme, "
                f"{_count(total, 'row')} in all.",
                "",
            ]
            for incident in items[:_MAX_INCIDENTS_PER_THEME]:
                out += [f"- {incident_sentence(incident)}"]
            if len(items) > _MAX_INCIDENTS_PER_THEME:
                rest = items[_MAX_INCIDENTS_PER_THEME:]
                out += [
                    f"- {_count(len(rest), 'smaller group')} accounted for another "
                    f"{_count(sum(i.count for i in rest), 'row')}."
                ]
            out += [""]
        if fixes:
            out += [
                f"{_count(len(fixes), 'pull request')} merged under this theme"
                + (
                    f"; the first {_MAX_FIXES_PER_THEME} are:"
                    if len(fixes) > _MAX_FIXES_PER_THEME
                    else ":"
                ),
                "",
            ]
            for pr in fixes[:_MAX_FIXES_PER_THEME]:
                out += [f"- {stamp(pr.stamp)}: {_pr_phrase(pr, clean)}"]
            out += [""]
    return "\n".join(out).rstrip() + "\n"


def _pr_phrase(pr: MergedPr, clean: Sanitizer) -> str:
    repository = pr.pr.rsplit("#", 1)[0]
    return clean.sanitize(f"{_snippet(pr.title, 160)} (in {repository})")


def _ruling_block(
    ruling: LedgerRow, prs: list[MergedPr], clean: Sanitizer
) -> list[str]:
    tickets = {ruling.get("mechanism-ticket"), ruling.get("ticket")} - {""}
    closing = [p for p in prs if p.ticket in tickets and p.stamp >= ruling.stamp]
    question = clean.sanitize(_snippet(ruling.get("question"), 300))
    kind = ruling.get("kind") or "decision"
    block = [f"### {stamp(ruling.stamp)}: {question or 'ruling'}", ""]
    decision = clean.sanitize(_snippet(ruling.text, 420)) if ruling.text else ""
    block += [
        f"This was a {kind} ruling"
        + (f" under {', '.join(sorted(tickets))}." if tickets else ".")
        + (
            f" What was decided: {decision}"
            if decision
            else " The ledger holds the operator's answer as a quotation, which this log leaves out; the row is stamped as above."
        )
    ]
    block += [""]
    if closing:
        shown = closing[:_MAX_PRS_PER_RULING]
        block += [
            f"{_count(len(closing), 'pull request')} merged under the same ticket after the ruling, "
            f"the first {len(shown)} being:"
            if len(closing) > len(shown)
            else f"{_count(len(closing), 'pull request')} merged under the same ticket after the ruling:",
            "",
        ]
        block += [f"- {stamp(p.stamp)}: {_pr_phrase(p, clean)}" for p in shown] + [""]
    else:
        block += ["No pull request under that ticket merged later in this week.", ""]
    return block


__all__ = ["Incident", "collect_incidents", "render_log", "stamp"]
