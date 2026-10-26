# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hostile-review runs queued or running on open PRs' current heads (pure, definition-B)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_runs_count import (
    ModelReviewerRunsCount,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_runs_facts import (
    ModelReviewerRunsFacts,
)

# The controller serves pending watcher CI reads for eight minutes
# (node_landing_watcher_read_compute.WATCHER_CI_MAX_AGE["PENDING"]).
REVIEWER_CI_MAX_AGE = timedelta(minutes=8)
# The summary gate consumes no voter capacity; only the reviewer job does.
REVIEWER_RUN_RE = re.compile(r"\bhostile reviewer\b", re.I)


def reviewer_runs_in_flight(
    records: Mapping[str, Mapping[str, Any]], *, now: str
) -> int:
    """Count fresh voter jobs on open, non-draft PR heads at the supplied decision time."""
    decision_at = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    n = 0
    for rec in records.values():
        facts = rec.get("facts") or {}
        ci = rec.get("ci") or {}
        if str(facts.get("state") or "").upper() != "OPEN" or facts.get("draft"):
            continue
        if not facts.get("head_sha") or ci.get("sha") != facts["head_sha"]:
            continue
        try:
            read_at = datetime.strptime(
                str(ci.get("read_at") or ""), "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC)
        except ValueError:
            continue
        if not timedelta(0) <= decision_at - read_at <= REVIEWER_CI_MAX_AGE:
            continue
        for r in ci.get("runs") or ():
            if (
                isinstance(r, (list, tuple))
                and len(r) >= 2
                and REVIEWER_RUN_RE.search(str(r[0]))
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
                },
                now=request.now,
            )
        )


__all__: list[str] = ["HandlerCountReviewerRunsInFlight", "reviewer_runs_in_flight"]
