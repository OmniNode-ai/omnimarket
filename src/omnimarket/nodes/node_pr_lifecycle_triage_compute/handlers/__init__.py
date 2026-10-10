# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers of the PR lifecycle triage compute node.

The handlers resolve on first use, not at import: ``HandlerClassifyHeadChecks`` reads the merge-control reason codes,
and the landing controller imports the red-rule handlers under a pydantic-only interpreter that holds this node and
nothing else (OMN-20745). ``from ...handlers import HandlerX`` works for each name below.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_apply_reviewer_pool import (
        HandlerApplyReviewerPool,
    )
    from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_cascade_checks import (
        HandlerClassifyCascadeChecks,
    )
    from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_companion_wait import (
        HandlerClassifyCompanionWait,
    )
    from omnimarket.nodes.node_pr_lifecycle_triage_compute.handlers.handler_classify_head_checks import (
        HandlerClassifyHeadChecks,
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

_HANDLER_MODULES: dict[str, str] = {
    "HandlerApplyReviewerPool": "handler_apply_reviewer_pool",
    "HandlerClassifyCascadeChecks": "handler_classify_cascade_checks",
    "HandlerClassifyCompanionWait": "handler_classify_companion_wait",
    "HandlerClassifyHeadChecks": "handler_classify_head_checks",
    "HandlerClassifyLandingRed": "handler_classify_landing_red",
    "HandlerClassifyStaleSummary": "handler_classify_stale_summary",
    "HandlerCountReviewerRunsInFlight": "handler_count_reviewer_runs_in_flight",
    "HandlerFiredEdgeGradedStale": "handler_fired_edge_graded_stale",
    "HandlerListCancelledChecks": "handler_list_cancelled_checks",
    "HandlerListPendingRequired": "handler_list_pending_required",
    "HandlerNextStaleStep": "handler_next_stale_step",
}

__all__: list[str] = [
    "HandlerApplyReviewerPool",
    "HandlerClassifyCascadeChecks",
    "HandlerClassifyCompanionWait",
    "HandlerClassifyHeadChecks",
    "HandlerClassifyLandingRed",
    "HandlerClassifyStaleSummary",
    "HandlerCountReviewerRunsInFlight",
    "HandlerFiredEdgeGradedStale",
    "HandlerListCancelledChecks",
    "HandlerListPendingRequired",
    "HandlerNextStaleStep",
]


def __getattr__(name: str) -> Any:
    module = _HANDLER_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{module}"), name)
