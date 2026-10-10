# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's red class of one head, in first-match order (pure, definition-B).

``classify_red`` is the controller's rule, unchanged: the ci-watch class of one head from its red check names
and the watcher's ``[name, status, conclusion, completed_at]`` rows. ``edge_fired`` is
``fired_edge_graded_stale``: every predecessor merged after this red was graded, so a red that includes a
change-control check is a stale grade and earns the one whole-run rerun (the landing decision caps it at one per
head; a red that survives it goes to a worker). ``cancelled`` is the head's cancelled check copies, passed only
under a decision node that declares the stale-refresh seam (OMN-20508): with no failed name and one of them, the
red is the cancelled copies', never ``product``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    REVIEWER_POOL_RE,
    SUMMARY_CHECK_RE,
    is_cascade_check,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.enum_landing_red_class import (
    EnumLandingRedClass,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_landing_red_facts import (
    ModelLandingRedFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_landing_red_verdict import (
    ModelLandingRedVerdict,
)


def classify_red(
    red: Iterable[str],
    runs: Iterable[Any],
    *,
    companion_merged: bool,
    edge_fired: bool = False,
    annotations: Mapping[str, str | None] | None = None,
    cancelled: Iterable[str] = (),
) -> str:
    """The ci-watch red class of one head, from its red check names and its runs."""
    names = [str(n) for n in red]
    runs = list(runs)
    concl = {
        str(r[0]): str(r[2] or "").lower()
        for r in runs
        if isinstance(r, (list, tuple)) and len(r) >= 3
    }
    real = [n for n in names if not SUMMARY_CHECK_RE.match(n)]
    if not real:
        real = names
    if not real:
        if tuple(cancelled):
            return "cancelled_producer"
        outcomes = {
            str(r[2] or "").lower()
            for r in runs
            if isinstance(r, (list, tuple)) and len(r) >= 3
        }
        if "cancelled" in outcomes and not outcomes & {
            "failure",
            "timed_out",
            "action_required",
            "startup_failure",
        }:
            return "cancelled_producer"
        return "product"
    if all(REVIEWER_POOL_RE.search(n) for n in real):
        return "reviewer_pool"
    real = [n for n in real if not REVIEWER_POOL_RE.search(n)]
    outcomes = {concl.get(n, "failure") for n in real}
    if outcomes <= {"cancelled"}:
        return "cancelled_producer"
    if outcomes <= {"timed_out", "cancelled"} and "timed_out" in outcomes:
        return "runner_saturation"
    if companion_merged and all(is_cascade_check(n, runs, annotations) for n in real):
        return "cascade"
    if edge_fired and any(is_cascade_check(n, runs, annotations) for n in real):
        return "cascade"
    return "product"


class HandlerClassifyLandingRed:
    """The red class of one head, and the reds a worker is told of (pure, definition-B)."""

    def handle(self, request: ModelLandingRedFacts) -> ModelLandingRedVerdict:
        return ModelLandingRedVerdict(
            red_class=EnumLandingRedClass(
                classify_red(
                    request.red,
                    request.runs,
                    companion_merged=request.companion_merged,
                    edge_fired=request.edge_fired,
                    annotations=request.annotations,
                    cancelled=request.cancelled,
                )
            ),
            worker_reds=tuple(n for n in request.red if not REVIEWER_POOL_RE.search(n)),
        )


__all__: list[str] = ["HandlerClassifyLandingRed", "classify_red"]
