# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fold the landing controller's stall transitions into trigger expectations.

A stalling transition (held, claim refused, attempt never ran; for a version
bump, anything short of merged or released) opens an expectation when none is
open for its subject, and only refreshes the decision text of one that is
open, so a hold repeated every tick keeps its first instant. A moving
transition answers the open expectation. The watchdog judges the result
against the overlay entry's answer deadline: unanswered past it is
TRIGGER_UNANSWERED, an episode, one alarm; answered closes it.
"""

from __future__ import annotations

from omnimarket.models.liveness.model_automation_liveness import (
    ModelTriggerExpectation,
)
from omnimarket.nodes.node_landing_stall_expectation_compute.models.model_landing_stall_fold import (
    ModelLandingStallFoldRequest,
    ModelLandingStallFoldResult,
)


class HandlerLandingStallExpectation:
    """Pure: the same transitions give the same expectations."""

    def handle(
        self, request: ModelLandingStallFoldRequest
    ) -> ModelLandingStallFoldResult:
        routes = {r.subject: r for r in request.routes}
        latest: dict[str, ModelTriggerExpectation] = {}
        unrouted: set[str] = set()
        for t in sorted(request.transitions, key=lambda t: t.observed_at):
            route = routes.get(t.subject)
            key = t.match_key
            if route is None:
                unrouted.add(key)
                continue
            detail = f"{t.transition.value}: {t.decision_text}"
            current = latest.get(key)
            is_open = current is not None and current.answered_at is None
            if t.answers:
                if current is not None and is_open:
                    latest[key] = current.model_copy(
                        update={"answered_at": t.observed_at, "detail": detail}
                    )
            elif current is not None and is_open:
                latest[key] = current.model_copy(update={"detail": detail})
            else:
                latest[key] = ModelTriggerExpectation(
                    process_id=route.process_id,
                    host=route.host,
                    match_key=key,
                    triggered_at=t.observed_at,
                    detail=detail,
                )
        return ModelLandingStallFoldResult(
            expectations=tuple(latest[k] for k in sorted(latest)),
            unrouted=tuple(sorted(unrouted)),
        )


__all__ = ["HandlerLandingStallExpectation"]
