# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The required contexts still running on a head (pure, definition-B).

A red PR whose required contexts are still running gets no worker yet (worker 2945 on omnibase_infra#4576 found only
``CI Summary`` pending). An unread required set fails closed (any pending check holds); a context pending longer than
``PENDING_STUCK_HOURS`` holds nothing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pending_required_facts import (
    ModelPendingRequiredFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pending_required_verdict import (
    ModelPendingRequiredVerdict,
)

PENDING_STUCK_HOURS = 2.0


def _hours_between(earlier: str, later: str) -> float | None:
    try:
        a = datetime.strptime(earlier, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(later, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds() / 3600


def pending_required_of(
    ci: Mapping[str, Any], head: str, required: Iterable[str] | None, *, now: str
) -> list[str]:
    """The required contexts still running on ``head`` (sorted), from the watcher record at that head.

    ``required`` None (unread) counts every pending check. A context whose newest copy started more than
    ``PENDING_STUCK_HOURS`` before ``now`` holds nothing, so a stuck run never parks a PR for good."""
    if not head or ci.get("sha") != head:
        return []
    started: dict[str, str] = {}
    names = {str(n) for n in ci.get("pending") or ()}
    for r in ci.get("runs") or ():
        if (
            isinstance(r, (list, tuple))
            and len(r) >= 2
            and str(r[1] or "").lower() != "completed"
        ):
            n = str(r[0])
            names.add(n)
            stamp = str((r[3] if len(r) > 3 else "") or "")
            if stamp and stamp > started.get(n, ""):
                started[n] = stamp
    if required is not None:
        names &= {str(n) for n in required}
    out = []
    for n in sorted(names):
        age = _hours_between(started[n], now) if started.get(n) else None
        if age is not None and age >= PENDING_STUCK_HOURS:
            continue
        out.append(n)
    return out


class HandlerListPendingRequired:
    """The required contexts still running on a head (pure, definition-B)."""

    def handle(self, request: ModelPendingRequiredFacts) -> ModelPendingRequiredVerdict:
        return ModelPendingRequiredVerdict(
            pending=tuple(
                pending_required_of(
                    request.ci.model_dump(exclude_unset=True),
                    request.head,
                    request.required,
                    now=request.now,
                )
            )
        )


__all__: list[str] = [
    "PENDING_STUCK_HOURS",
    "HandlerListPendingRequired",
    "pending_required_of",
]
