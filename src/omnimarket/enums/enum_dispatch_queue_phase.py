# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Durable per-item lifecycle phases for the legacy dispatch queue (OMN-17018).

Before this taxonomy existed the queue had no progress operator: the drainer
selected the oldest item, compiled it, wrote a result artifact and left the
file exactly as it found it, so every subsequent run re-selected the same item
forever. "Compiled" stood in for "executed" and nothing advanced.
"""

from __future__ import annotations

from enum import StrEnum, unique


@unique
class EnumDispatchQueuePhase(StrEnum):
    """Phases a queue item moves through, in order."""

    #: On disk, never claimed. The only phase that is selectable for draining.
    QUEUED = "queued"
    #: A drainer holds a renewable lease on the item. Expiry marks the claim
    #: stale; it never deletes the item and never silently returns it to QUEUED.
    CLAIMED = "claimed"
    #: A dispatch command was compiled and handed off. Awaiting acknowledgement
    #: until ``ack_deadline``; past it the item is observably PENDING.
    DISPATCHED = "dispatched"
    #: The dispatched lane acknowledged that it started.
    STARTED = "started"
    #: The item is finished — completed, or stopped with a typed reason.
    TERMINAL = "terminal"


#: Phases in which an item is held by an in-flight attempt and is therefore
#: never re-selected as if untouched.
IN_FLIGHT_PHASES: frozenset[EnumDispatchQueuePhase] = frozenset(
    {
        EnumDispatchQueuePhase.CLAIMED,
        EnumDispatchQueuePhase.DISPATCHED,
        EnumDispatchQueuePhase.STARTED,
    }
)


__all__: list[str] = ["IN_FLIGHT_PHASES", "EnumDispatchQueuePhase"]
