# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Judge a ticket the store read: carrying the task's Floor key line, open, not an umbrella, not visible to the old fill.

A pure function of what the store returned. ``old-fill-visible`` is the old fill's own view of a ticket: it sees a
ticket that has a project, a label naming delegation or a pillar, so the mint never uses or replaces one.
"""

from __future__ import annotations

import re
from typing import Literal

from omnibase_spi.contracts.services import ModelFloorTicketLookupResult

FLOOR_KEY_LABEL = "Floor key:"
DELEGATION_WORD = "delegation"

Judgement = Literal[
    "usable", "wrong-key", "canceled", "done", "umbrella", "old-fill-visible"
]

_DONE = frozenset({"done", "completed"})
_CANCELED = frozenset({"canceled", "cancelled", "duplicate"})
_NUMBER = re.compile(r"([0-9]+)$")


def carries_floor_key_line(description: str | None, floor_key: str) -> bool:
    """Whether the description holds the exact line ``Floor key: <floor_key>`` (a longer key does not match)."""
    wanted = f"{FLOOR_KEY_LABEL} {floor_key}"
    return any(line.strip() == wanted for line in (description or "").splitlines())


def visible_to_old_fill(found: ModelFloorTicketLookupResult) -> bool:
    """Whether the old fill can see the ticket: a project, a label naming delegation or a pillar."""
    return (
        bool((found.project or "").strip())
        or bool((found.pillar or "").strip())
        or any(DELEGATION_WORD in label.lower() for label in found.labels)
    )


def judge(found: ModelFloorTicketLookupResult, floor_key: str) -> Judgement:
    """The first reason a found ticket is not the task's usable ticket, or ``usable``."""
    if not carries_floor_key_line(found.description, floor_key):
        return "wrong-key"
    status = (found.status or "").strip().lower()
    if status in _DONE:
        return "done"
    if status in _CANCELED:
        return "canceled"
    if found.has_children:
        return "umbrella"
    if visible_to_old_fill(found):
        return "old-fill-visible"
    return "usable"


def oldest_first(tickets: tuple[str, ...]) -> tuple[str, ...]:
    """The tickets in creation order: the lowest number first, an id with no number after the others."""

    def number(ticket: str) -> tuple[int, int, str]:
        match = _NUMBER.search(ticket)
        return (0, int(match.group(1)), ticket) if match else (1, 0, ticket)

    return tuple(sorted(dict.fromkeys(tickets), key=number))
