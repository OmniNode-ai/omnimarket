# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The hostile-review pool rule: which reviewer-pool reds earn their one rerun now (pure, definition-B).

Turns every ``reviewer_pool`` red into something the landing decision knows (OMN-20067). Oldest first, while the
reviewer endpoint has free slots, a head the decision has not rerun is presented as ``runner_saturation``: the
decision reruns it once and records the head. Every other reviewer-pool red (already rerun at this head, or no
free slot) is presented as ``pending``: no merge, no worker, and no rerun until a slot frees or the head moves.
The decision never sees ``reviewer_pool``.
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


def apply_reviewer_pool(
    prs: Iterable[dict[str, Any]],
    *,
    state_records: Iterable[Mapping[str, Any]],
    in_flight: int,
    slots: int = REVIEWER_SLOTS,
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
        if budget > 0 and p["head_sha"] not in rerun.get(p["pr"], set()):
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
        )
        return ModelReviewerPoolResult(
            prs=tuple(ModelReviewerPoolPr.model_validate(p) for p in out)
        )


__all__: list[str] = [
    "REVIEWER_SLOTS",
    "HandlerApplyReviewerPool",
    "apply_reviewer_pool",
]
