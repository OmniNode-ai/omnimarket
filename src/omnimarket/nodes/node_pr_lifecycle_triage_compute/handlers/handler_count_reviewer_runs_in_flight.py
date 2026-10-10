# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile-review runs queued or running on open PRs' current heads (pure, definition-B)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    REVIEWER_POOL_RE,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_runs_count import (
    ModelReviewerRunsCount,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_runs_facts import (
    ModelReviewerRunsFacts,
)


def reviewer_runs_in_flight(records: Mapping[str, Mapping[str, Any]]) -> int:
    """Hostile-review runs queued or running on open PRs' current heads, from the watcher's records."""
    n = 0
    for rec in records.values():
        if str((rec.get("facts") or {}).get("state") or "").upper() != "OPEN":
            continue
        for r in (rec.get("ci") or {}).get("runs") or ():
            if (
                isinstance(r, (list, tuple))
                and len(r) >= 2
                and REVIEWER_POOL_RE.search(str(r[0]))
            ):
                n += str(r[1]).lower() in (
                    "queued",
                    "in_progress",
                    "waiting",
                    "pending",
                    "requested",
                )
    return n


class HandlerCountReviewerRunsInFlight:
    """The reviewer endpoint's busy slots, from the watcher's records (pure, definition-B)."""

    def handle(self, request: ModelReviewerRunsFacts) -> ModelReviewerRunsCount:
        return ModelReviewerRunsCount(
            in_flight=reviewer_runs_in_flight(
                {
                    short: rec.model_dump(exclude_unset=True)
                    for short, rec in request.records.items()
                }
            )
        )


__all__: list[str] = ["HandlerCountReviewerRunsInFlight", "reviewer_runs_in_flight"]
