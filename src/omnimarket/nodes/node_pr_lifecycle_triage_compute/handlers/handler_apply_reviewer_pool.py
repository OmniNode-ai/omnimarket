# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile-review pool rule: which reviewer-pool reds earn their one rerun now (pure, definition-B).

Turns every ``reviewer_pool`` red into something the landing decision knows (OMN-20067). Oldest first, while the
reviewer endpoint has free slots, a head the decision has not rerun is presented as ``runner_saturation``: the
decision reruns it once and records the head. A head with no free slot is presented as ``pending``: no merge, no
worker, and no rerun until a slot frees. A head already rerun is past its one rerun, so a reviewer red at it is the
reviewer's verdict on the code: it is presented as ``product`` with its reviewer checks, and the decision sends a
fix worker (whose CI context carries the reviewer's blocking findings) or escalates once the worker ladder is spent
(OMN-19998: omniclaude#2752 sat pending at its head with no worker and no escalation). It needs no slot. The
decision never sees ``reviewer_pool``.

Review last (OMN-19998): a review receipt is bound to one head and a rerun spends reviewer capacity, so a head that
the controller is about to change gets no rerun yet. A head whose merge state is ``conflicting`` or ``behind``, or that
a worker holds a lease on (``leased``), is presented as ``pending`` without taking a slot: the decision updates or
resolves it first (the conflict and behind briefs) and the new head is reviewed once, last. Measured 2026-11-05:
omnimarket#5097 was reviewed at 14:33Z on a head a conflict worker had held since 14:20Z and rewrote at 14:44Z, and
omnimarket#5103 was reviewed on a head that conflicted with the dev merge of 14:13Z.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_facts import (
    ModelReviewerPoolFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_pr import (
    ModelReviewerPoolPr,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_result import (
    ModelReviewerPoolResult,
)

# The reviewer endpoint's concurrent slots: 13 concurrent reruns timed out against its 4 llama.cpp slots.
REVIEWER_SLOTS = 4
# A head in one of these merge states changes before it can merge: its review would be bound to a head that goes.
REVIEW_AFTER_MERGE_STATES = frozenset({"conflicting", "behind"})


def review_wait_reason(
    pr: Mapping[str, Any], wait_states: Iterable[str] = REVIEW_AFTER_MERGE_STATES
) -> str | None:
    """Why this head must change before it is reviewed, or None when its review may go now (pure)."""
    if pr.get("leased"):
        return "leased"
    state = str(pr.get("merge_state") or "")
    return state if state in set(wait_states) else None


def apply_reviewer_pool(
    prs: Iterable[dict[str, Any]],
    *,
    state_records: Iterable[Mapping[str, Any]],
    in_flight: int,
    slots: int = REVIEWER_SLOTS,
    wait_states: Iterable[str] = REVIEW_AFTER_MERGE_STATES,
) -> list[dict[str, Any]]:
    """Turn every ``reviewer_pool`` red into something the decision node knows (OMN-20067)."""
    rerun: dict[str, set[str]] = {
        str(r.get("pr")): {str(h) for h in r.get("rerun_heads") or ()}
        for r in state_records
    }
    budget = max(0, slots - in_flight)
    out = [dict(p) for p in prs]
    for p in sorted(
        (p for p in out if p.get("red_class") == "reviewer_pool"),
        key=lambda p: (p["created_at"], p["pr"]),
    ):
        if p["head_sha"] in rerun.get(p["pr"], set()):
            p["red_class"] = "product"
        elif review_wait_reason(p, wait_states) is not None:
            p["ci"], p["red_class"], p["red_checks"] = "pending", None, []
        elif budget > 0:
            p["red_class"] = "runner_saturation"
            budget -= 1
        else:
            p["ci"], p["red_class"], p["red_checks"] = "pending", None, []
    return out


class HandlerApplyReviewerPool:
    """The reviewer-pool rule over the open PRs' facts (pure, definition-B)."""

    def handle(self, request: ModelReviewerPoolFacts) -> ModelReviewerPoolResult:
        out = apply_reviewer_pool(
            [p.model_dump(exclude_unset=True) for p in request.prs],
            state_records=[
                r.model_dump(exclude_unset=True) for r in request.state_records
            ],
            in_flight=request.in_flight,
            slots=request.slots,
            wait_states=request.wait_merge_states,
        )
        return ModelReviewerPoolResult(
            prs=tuple(ModelReviewerPoolPr.model_validate(p) for p in out)
        )


__all__: list[str] = [
    "REVIEWER_SLOTS",
    "REVIEW_AFTER_MERGE_STATES",
    "HandlerApplyReviewerPool",
    "apply_reviewer_pool",
    "review_wait_reason",
]
