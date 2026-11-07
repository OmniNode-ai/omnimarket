# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Close the starts that a process left open and could not close itself (OMN-22864).

Every way out of a cycle that can still run code ends its start in a success or a typed failure. A process that is killed
(the publish step's time limit, a host restart, a crash) cannot: its start stays in the append-only journal with no
terminal, and the floor reads ``start_without_terminal`` on it for the whole window the receipts look back over. The next
cycle closes such a start with a typed failure under the chain's own correlation id, so the journal says what happened to it.

A start is abandoned only when it is older than the contract's ``abandon_after_seconds``, which is above the longest time
the publish node lets a cycle live: a cycle still running is never closed from under itself.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta

from omnimarket.models.floor_17a import ModelNodeEmission

ABANDONED_CODE = "abandoned_without_terminal"
ABANDONED_DETAIL = (
    "the process that emitted this start ended without a success or a typed failure "
    "(killed, restarted or crashed); closed by a later cycle"
)


def abandoned_starts(
    events: Iterable[ModelNodeEmission],
    *,
    now: datetime,
    abandon_after_s: int,
    piece: int,
    nodes: frozenset[str],
) -> tuple[ModelNodeEmission, ...]:
    """The typed failures that close each start of ``nodes`` on ``piece`` that has no terminal and is older than the bound."""
    chains: dict[tuple[str, str], list[ModelNodeEmission]] = defaultdict(list)
    for event in events:
        if event.piece == piece and event.node in nodes:
            chains[(event.node, event.correlation_id)].append(event)
    limit = timedelta(seconds=abandon_after_s)
    closed: list[ModelNodeEmission] = []
    for (node, cid), chain in sorted(chains.items()):
        starts = [e for e in chain if e.phase == "start"]
        if not starts or any(e.phase != "start" for e in chain):
            continue
        first = min(starts, key=lambda e: e.emitted_at)
        if now - first.emitted_at <= limit:
            continue
        closed.append(
            ModelNodeEmission(
                node=node,
                piece=piece,
                correlation_id=cid,
                phase="failure",
                emitted_at=now,
                backend=first.backend,
                drill=first.drill,
                failure_code=ABANDONED_CODE,
                failure_detail=ABANDONED_DETAIL,
            )
        )
    return tuple(closed)
