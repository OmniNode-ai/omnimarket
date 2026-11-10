# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the closer remembers of each examination, and the rules that read it (OMN-23282).

A ticket the closer rejects again with the same reason posts no new comment, so the newest closer
comment's time never moved and the oldest-comment order picked the same unflippable tickets every
fire. The selection now records each ticket it picks: when, and the ticket's *state key* (its
description hash, the time of its newest closer comment and whether it has a contract). A later
run that reads the same key knows the examination left nothing new: the rejection is unchanged.
Those tickets rank by when they were last examined and, once the key has held for the overlay's
number of selections, are held until it changes. Performs no read.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnimarket.nodes.node_dod_closeout_sweep_compute.models.model_closeout_scope_overlay import (
    ModelCloseoutScopeOverlay,
)


def state_key(row: Mapping[str, Any]) -> str:
    """The ticket's state as the closer reads it now. Empty when nothing was ever posted: a
    ticket the closer has not commented on has no rejection to call unchanged."""
    commented = row.get("last_closeout_comment_at")
    if not isinstance(commented, str) or not commented:
        return ""
    text = row.get("text_sha")
    contract = row.get("has_contract")
    return f"{text if isinstance(text, str) else ''}|{commented}|{contract is True}"


def _entry(memory: Mapping[str, Mapping[str, Any]], ticket: Any) -> Mapping[str, Any]:
    entry = memory.get(ticket) if isinstance(ticket, str) else None
    return entry if isinstance(entry, Mapping) else {}


def with_examined_state(
    rows: Sequence[dict[str, Any]], memory: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Each row carries ``last_examined_at``, the later of its newest closer comment and the
    memory's last selection, and ``examined_unchanged``, the number of consecutive selections
    that left its state key as it reads now (0 when the key changed or none was recorded)."""
    out: list[dict[str, Any]] = []
    for row in rows:
        entry = _entry(memory, row.get("id"))
        comment = row.get("last_closeout_comment_at")
        commented = comment if isinstance(comment, str) else ""
        examined = entry.get("examined_at")
        examined_at = examined if isinstance(examined, str) else ""
        key = state_key(row)
        streak = entry.get("streak")
        unchanged = (
            streak
            if key
            and entry.get("key") == key
            and isinstance(streak, int)
            and streak > 0
            else 0
        )
        out.append(
            {
                **row,
                "last_examined_at": max(commented, examined_at),
                "examined_unchanged": unchanged,
            }
        )
    return out


def held_for_unchanged_rejection(
    row: Mapping[str, Any], overlay: ModelCloseoutScopeOverlay
) -> bool:
    """Held until the description, the newest closer comment or the contract state changes. A
    ticket waiting on a post-merge condition is never held: that condition moves on its own."""
    waiting_on = row.get("waiting_on")
    if isinstance(waiting_on, list) and waiting_on:
        return False
    unchanged = row.get("examined_unchanged")
    return (
        isinstance(unchanged, int)
        and unchanged >= overlay.hold_after_unchanged_selections
    )


def examined_update(
    selected: Sequence[Mapping[str, Any]], now: str
) -> dict[str, dict[str, Any]]:
    """The memory entry of each ticket just selected: examined now, its state key as read, and
    the count of consecutive selections that left that key unchanged."""
    update: dict[str, dict[str, Any]] = {}
    for row in selected:
        key = state_key(row)
        unchanged = row.get("examined_unchanged")
        count = unchanged if isinstance(unchanged, int) else 0
        update[row["id"]] = {
            "examined_at": now,
            "key": key,
            "streak": count + 1 if key else 0,
        }
    return update


def next_memory(
    memory: Mapping[str, Mapping[str, Any]],
    scope_ids: Sequence[str],
    update: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The memory after a selection: this run's update over what was recorded, for the tickets
    still in scope only (a ticket that left the scope forgets its examinations)."""
    keep = set(scope_ids)
    merged = {ticket: dict(entry) for ticket, entry in memory.items() if ticket in keep}
    merged.update({ticket: dict(entry) for ticket, entry in update.items()})
    return dict(sorted(merged.items()))
