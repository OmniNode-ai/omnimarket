# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The landing controller's old function signatures over the node's pure handlers.

The controller's tests called ``landing_facts.classify_red(red, runs, companion_merged=...)`` and its siblings.
This adapter keeps those call shapes and answers each through the handler's ``handle(request)``, so a test ported
from the controller asserts the same verdicts on the same facts through the node.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_apply_reviewer_pool import (
    HandlerApplyReviewerPool,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
    HandlerClassifyCascadeChecks,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_companion_wait import (
    HandlerClassifyCompanionWait,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_landing_red import (
    HandlerClassifyLandingRed,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_stale_summary import (
    HandlerClassifyStaleSummary,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_count_reviewer_runs_in_flight import (
    HandlerCountReviewerRunsInFlight,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_fired_edge_graded_stale import (
    HandlerFiredEdgeGradedStale,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_list_cancelled_checks import (
    HandlerListCancelledChecks,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_list_pending_required import (
    HandlerListPendingRequired,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_next_stale_step import (
    HandlerNextStaleStep,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cancelled_checks_facts import (
    ModelCancelledChecksFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cascade_check_facts import (
    ModelCascadeCheckFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_cascade_check_verdict import (
    ModelCascadeCheckVerdict,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_companion_wait_facts import (
    ModelCompanionWaitFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_fired_edge_facts import (
    ModelFiredEdgeFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_landing_red_facts import (
    ModelLandingRedFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_pending_required_facts import (
    ModelPendingRequiredFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_pool_facts import (
    ModelReviewerPoolFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_reviewer_runs_facts import (
    ModelReviewerRunsFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_step_facts import (
    ModelStaleStepFacts,
)
from omnimarket.nodes.node_pr_lifecycle_triage_compute.models.model_stale_summary_facts import (
    ModelStaleSummaryFacts,
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
    request = ModelLandingRedFacts.model_validate(
        {
            "red": list(red),
            "runs": [list(r) for r in runs],
            "companion_merged": companion_merged,
            "edge_fired": edge_fired,
            "annotations": None if annotations is None else dict(annotations),
            "cancelled": list(cancelled),
        }
    )
    return HandlerClassifyLandingRed().handle(request).red_class.value


def worker_reds(red: Iterable[str], runs: Iterable[Any] = ()) -> tuple[str, ...]:
    """The reds a worker's brief lists: the given names without the hostile-review family."""
    request = ModelLandingRedFacts.model_validate(
        {"red": list(red), "runs": [list(r) for r in runs], "companion_merged": False}
    )
    return HandlerClassifyLandingRed().handle(request).worker_reds


def _cascade(
    red: Iterable[str],
    runs: Iterable[Any],
    annotations: Mapping[str, str | None] | None,
) -> ModelCascadeCheckVerdict:
    request = ModelCascadeCheckFacts.model_validate(
        {
            "red": list(red),
            "runs": [list(r) for r in runs],
            "annotations": None if annotations is None else dict(annotations),
        }
    )
    return HandlerClassifyCascadeChecks().handle(request)


def is_cascade_check(
    name: str,
    runs: Iterable[Any] = (),
    annotations: Mapping[str, str | None] | None = None,
) -> bool:
    return name in _cascade((name,), runs, annotations).cascade_checks


def cascade_only(
    red: Iterable[str],
    runs: Iterable[Any] = (),
    annotations: Mapping[str, str | None] | None = None,
) -> bool:
    return _cascade(red, runs, annotations).cascade_only


def cancelled_of(ci: Mapping[str, Any], red: Iterable[str] = ()) -> tuple[str, ...]:
    request = ModelCancelledChecksFacts.model_validate(
        {"ci": dict(ci), "red": list(red)}
    )
    return HandlerListCancelledChecks().handle(request).cancelled


def fired_edge_graded_stale(
    parent_states: Mapping[str, Mapping[str, Any]], last_red_completed_at: str
) -> bool:
    request = ModelFiredEdgeFacts.model_validate(
        {
            "parent_states": {k: dict(v) for k, v in parent_states.items()},
            "last_red_completed_at": last_red_completed_at,
        }
    )
    return HandlerFiredEdgeGradedStale().handle(request).stale


def apply_reviewer_pool(
    prs: Iterable[dict[str, Any]],
    *,
    state_records: Iterable[Mapping[str, Any]],
    in_flight: int,
    slots: int = 4,
) -> list[dict[str, Any]]:
    request = ModelReviewerPoolFacts.model_validate(
        {
            "prs": list(prs),
            "state_records": [dict(r) for r in state_records],
            "in_flight": in_flight,
            "slots": slots,
        }
    )
    return [
        p.model_dump(exclude_unset=True)
        for p in HandlerApplyReviewerPool().handle(request).prs
    ]


def reviewer_runs_in_flight(records: Mapping[str, Mapping[str, Any]]) -> int:
    request = ModelReviewerRunsFacts.model_validate(
        {"records": {k: dict(v) for k, v in records.items()}}
    )
    return HandlerCountReviewerRunsInFlight().handle(request).in_flight


def companion_wait_of(
    short: str,
    rec: Mapping[str, Any],
    live: Any,
    records: Mapping[str, Mapping[str, Any]],
    annotations: Mapping[str, str | None] | None = None,
) -> dict[str, Any] | None:
    request = ModelCompanionWaitFacts.model_validate(
        {
            "short": short,
            "rec": dict(rec),
            "live": None if live is None else vars(live),
            "records": {k: dict(v) for k, v in records.items()},
            "annotations": None if annotations is None else dict(annotations),
        }
    )
    verdict = HandlerClassifyCompanionWait().handle(request)
    if not verdict.waiting:
        return None
    return {
        "companion": verdict.companion,
        "companion_red": list(verdict.companion_red),
    }


def stale_summary_of(ci: Mapping[str, Any], head: str) -> str | None:
    kind = (
        HandlerClassifyStaleSummary()
        .handle(ModelStaleSummaryFacts.model_validate({"ci": dict(ci), "head": head}))
        .kind
    )
    return None if kind is None else kind.value


def stale_step(kind: str, mem: Mapping[str, Any] | None, head: str) -> str | None:
    request = ModelStaleStepFacts.model_validate(
        {"kind": kind, "mem": None if mem is None else dict(mem), "head": head}
    )
    step = HandlerNextStaleStep().handle(request).step
    return None if step is None else step.value


def pending_required_of(
    ci: Mapping[str, Any], head: str, required: Iterable[str] | None, *, now: str
) -> list[str]:
    request = ModelPendingRequiredFacts.model_validate(
        {
            "ci": dict(ci),
            "head": head,
            "required": None if required is None else list(required),
            "now": now,
        }
    )
    return list(HandlerListPendingRequired().handle(request).pending)


def stale_memory_next(
    mem: Mapping[str, Any] | None, head: str, step: str, *, now: str
) -> dict[str, Any]:
    """The controller's sidecar write after ``step`` at ``head``; the controller keeps it, tests need the memory."""
    m = mem or {}
    done = [*((m.get("heads") or {}).get(head) or ()), step]
    refreshes = int(m.get("refreshes") or 0) + (1 if step == "update_branch" else 0)
    return {"heads": {head: done}, "refreshes": refreshes, "at": now}
