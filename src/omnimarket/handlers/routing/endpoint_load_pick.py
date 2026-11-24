# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Pure least-busy pick over a load snapshot (OMN-26534).

The spread pick of ``backend_placement`` shares a rung's traffic by a stable hash
of the correlation id and reads no load, so a request can queue behind a busy
model server while a peer serving the same model sits idle. This is the pick
that reads the load: given the group's ``backend_ref`` list in declaration order
and a snapshot, it names the member with the fewest waiting requests.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.models.delegation.model_endpoint_load_snapshot import (
    ModelEndpointLoadSnapshot,
)


def pick_least_busy_index(
    backend_refs: Sequence[str], load: ModelEndpointLoadSnapshot | None
) -> int | None:
    """Index of the readable member with the fewest waiting requests.

    A member the snapshot does not name, or names with no reading, is skipped,
    so an unreachable or unhealthy endpoint is never chosen while another can be
    read. Ties resolve in declaration order (the earliest index wins). Returns
    ``None`` when there is no snapshot or no member is readable, and the caller
    keeps its existing pick: a blind snapshot never fails a request.
    """
    if load is None:
        return None
    best: int | None = None
    best_waiting = 0
    for index, ref in enumerate(backend_refs):
        reading = load.load_of(ref)
        if reading is None or reading.waiting is None:
            continue
        if best is None or reading.waiting < best_waiting:
            best, best_waiting = index, reading.waiting
    return best


__all__: list[str] = ["pick_least_busy_index"]
