"""Read which green heads the landing controller's newest facts say are not green-landable (OMN-19998).

On 2026-10-19 two PRs of a plugin repository (heads e7bac5a5f13a and 63328046f6e3) were listed green-unlanded with
merge_state UNKNOWN and lane controller-fix-green-unlanded-20261019T0433Z was dispatched for them. main had just merged
#1001, so GitHub was recomputing mergeability and the PR watcher read no merge state; controller tick 3169 had read both
conflicting at those heads and held conflict workers on them (leases 11152 and 11142). omnimarket#4138 (head
248f3402a610) was listed green for 171m while tick 3170 read it red on four required contexts that never started
(absent_required) and leased worker 11162: the watcher counts only the check runs that exist. A head the controller's
fresh facts read conflicting, or red on named checks, at the watcher's head is not green-landable, and the controller is
acting on it. Pure: this handler reads nothing and writes nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .models import ModelGreenControllerReadRequest, ModelGreenControllerReadResult


def _parse(stamp: Any) -> datetime | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    try:
        at = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else None


def _not_green(entry: Mapping[str, Any]) -> bool:
    return entry.get("merge_state") == "conflicting" or (
        entry.get("ci") == "red" and bool(entry.get("red_checks"))
    )


class HandlerGreenControllerRead:
    def handle(
        self, request: ModelGreenControllerReadRequest
    ) -> ModelGreenControllerReadResult:
        facts = request.facts if isinstance(request.facts, Mapping) else {}
        entries = facts.get("prs")
        if not isinstance(entries, list):
            return ModelGreenControllerReadResult()
        if request.now is not None and request.bound_min is not None:
            observed = _parse(facts.get("observed_at"))
            if (
                observed is None
                or (request.now - observed).total_seconds() / 60 > request.bound_min
            ):
                return ModelGreenControllerReadResult()
        out = set()
        for entry in entries:
            if not isinstance(entry, Mapping):
                continue
            short = str(entry.get("pr")).rsplit("/", 1)[-1]
            if (
                short in request.heads
                and entry.get("head_sha") == request.heads[short]
                and _not_green(entry)
            ):
                out.add(short)
        return ModelGreenControllerReadResult(not_green=frozenset(out))
