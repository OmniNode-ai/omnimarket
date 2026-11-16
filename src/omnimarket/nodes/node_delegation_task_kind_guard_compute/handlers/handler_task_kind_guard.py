# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decide whether one local answer to a classification question is accepted or escalates (OMN-24296).

Follows the shape of handler_delegation_routing_order: one typed request, one
typed answer, each fault a stable refusal. No number, kind name or backend
name is written here: the route (declared kinds, local backend, guard sample
size and accuracy lower bound) comes from the routing overlay's
``task_kind_rungs`` section. The answer is scored against the deterministic
class when the caller has one, and the running tally the caller carries
decides when the guard falls the question back to its current rung.
"""

from __future__ import annotations

from collections.abc import Callable

from omnibase_infra.errors import ProtocolConfigurationError

from omnimarket.handlers.routing.routing_tiers_path import (
    load_delegation_routing_overlay,
)
from omnimarket.models.delegation.model_delegation_routing_overlay import (
    EMPTY_DELEGATION_ROUTING_OVERLAY,
    ModelDelegationRoutingOverlay,
)
from omnimarket.models.delegation.model_task_kind_rungs_overlay import (
    EnumTaskKindRung,
    ModelTaskKindRouteSet,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.enum_task_kind_guard import (
    EnumTaskKindGuardReason,
    EnumTaskKindGuardRefusal,
    EnumTaskKindGuardRoute,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_answer import (
    ModelTaskKindGuardAnswer,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_request import (
    ModelTaskKindGuardRequest,
)
from omnimarket.nodes.node_delegation_task_kind_guard_compute.models.model_task_kind_guard_tally import (
    ModelTaskKindGuardTally,
)


class HandlerDelegationTaskKindGuard:
    """Decide whether a local answer is accepted or escalates."""

    def __init__(
        self,
        load_overlay: Callable[
            [], ModelDelegationRoutingOverlay
        ] = load_delegation_routing_overlay,
    ) -> None:
        self._load_overlay = load_overlay

    def handle(self, request: ModelTaskKindGuardRequest) -> ModelTaskKindGuardAnswer:
        try:
            overlay = self._load_overlay()
        except ProtocolConfigurationError as exc:
            return self._refusal(
                EnumTaskKindGuardRefusal.OVERLAY_INVALID, str(exc), request.tally
            )
        if (
            overlay == EMPTY_DELEGATION_ROUTING_OVERLAY
            or overlay.task_kind_rungs is None
        ):
            return self._refusal(
                EnumTaskKindGuardRefusal.OVERLAY_UNBOUND,
                "the routing overlay is unbound or has no task_kind_rungs",
                request.tally,
            )
        route = overlay.task_kind_rungs.route_for(request.task_class, request.task_kind)
        if route is None:
            return self._refusal(
                EnumTaskKindGuardRefusal.NO_ROUTE,
                f"no task_kind route for task_class {request.task_class!r} "
                f"task_kind {request.task_kind!r}",
                request.tally,
            )
        return self._decide(request, route)

    def _decide(
        self,
        request: ModelTaskKindGuardRequest,
        route: ModelTaskKindRouteSet,
    ) -> ModelTaskKindGuardAnswer:
        rung = route.rung_for(request.answered_kind)
        tally = request.tally
        scored_correct: bool | None = None
        if request.deterministic_kind is not None:
            scored_correct = request.answered_kind == request.deterministic_kind
            tally = ModelTaskKindGuardTally(
                scored=tally.scored + 1,
                correct=tally.correct + (1 if scored_correct else 0),
            )
        accuracy: float | None = None
        if tally.scored > 0:
            accuracy = tally.correct / tally.scored
        guard_tripped = (
            tally.scored >= route.guard.min_samples
            and accuracy is not None
            and accuracy < route.guard.accuracy_lower_bound
        )
        if guard_tripped:
            route_out = EnumTaskKindGuardRoute.ESCALATE
            reason = EnumTaskKindGuardReason.GUARD_TRIPPED
        elif scored_correct is False:
            route_out = EnumTaskKindGuardRoute.ESCALATE
            reason = EnumTaskKindGuardReason.DETERMINISTIC_DISAGREES
        elif rung is None:
            route_out = EnumTaskKindGuardRoute.ESCALATE
            reason = EnumTaskKindGuardReason.KIND_UNDECLARED
        elif rung is EnumTaskKindRung.CURRENT:
            route_out = EnumTaskKindGuardRoute.ESCALATE
            reason = EnumTaskKindGuardReason.KIND_STAYS_ON_CURRENT
        else:
            route_out = EnumTaskKindGuardRoute.LOCAL
            reason = EnumTaskKindGuardReason.LOCAL_ACCEPTED
        return ModelTaskKindGuardAnswer(
            ok=True,
            route=route_out,
            reason=reason,
            refusal=None,
            detail="",
            tally=tally,
            scored_correct=scored_correct,
            local_backend_id=route.local_backend_id,
            accuracy=accuracy,
            accuracy_lower_bound=route.guard.accuracy_lower_bound,
        )

    def _refusal(
        self,
        refusal: EnumTaskKindGuardRefusal,
        detail: str,
        tally: ModelTaskKindGuardTally,
    ) -> ModelTaskKindGuardAnswer:
        return ModelTaskKindGuardAnswer(
            ok=False,
            route=None,
            reason=None,
            refusal=refusal,
            detail=detail,
            tally=tally,
            scored_correct=None,
            local_backend_id=None,
            accuracy=None,
            accuracy_lower_bound=None,
        )


__all__: list[str] = ["HandlerDelegationTaskKindGuard"]
