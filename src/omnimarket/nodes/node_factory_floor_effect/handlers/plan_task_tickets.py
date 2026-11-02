# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The tickets a plan's own prose pairs with its task ids (``T0.1 OMN-21494, T2.6 OMN-21496 to OMN-21499``) (OMN-21930)."""

from __future__ import annotations

import re

TASK_ID = r"[A-Z]{1,4}\.?\d+(?:\.\d+)*[a-z]?"
TICKET = r"OMN-\d+"
# A task id, then (optionally ``+ K2``) a ticket straight after it, then more tickets joined by a comma, "and" or "to".
PAIR_RE = re.compile(
    rf"(?<![\w.])(?P<task>{TASK_ID})(?![\w.])(?:\s*\+\s*[A-Z]\w*)?[\s(:]+"
    rf"(?P<tickets>{TICKET}(?:(?:\s*,\s*|\s+and\s+|\s+to\s+){TICKET})*)"
)
LIST_SPLIT_RE = re.compile(r"(\s*,\s*|\s+and\s+|\s+to\s+)")
MAX_RANGE = 20


def _expand(joined: str) -> tuple[str, ...]:
    parts = LIST_SPLIT_RE.split(joined)
    tickets: list[str] = [parts[0]]
    for separator, ticket in zip(parts[1::2], parts[2::2], strict=True):
        if separator.strip() == "to":
            low, high = int(tickets[-1].split("-")[1]), int(ticket.split("-")[1])
            if 0 < high - low <= MAX_RANGE:
                tickets.extend(f"OMN-{n}" for n in range(low + 1, high))
        tickets.append(ticket)
    return tuple(tickets)


def prose_task_tickets(text: str, epic: str = "") -> dict[str, tuple[str, ...]]:
    """Each task id the plan's text pairs with tickets, to those tickets; the plan's own epic belongs to no task."""
    found: dict[str, list[str]] = {}
    for match in PAIR_RE.finditer(text):
        tickets = found.setdefault(match.group("task"), [])
        tickets.extend(
            ticket
            for ticket in _expand(match.group("tickets"))
            if ticket != epic and ticket not in tickets
        )
    return {task: tuple(tickets) for task, tickets in found.items() if tickets}


def merge_task_links(
    *links: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    """Task links from several sources, a task's tickets in the order the sources come."""
    merged: dict[str, tuple[str, ...]] = {}
    for source in links:
        for task, tickets in source.items():
            merged[task] = tuple(dict.fromkeys((*merged.get(task, ()), *tickets)))
    return merged
