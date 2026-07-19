# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill dispatch handler for node_skill_dispatch_engine_orchestrator.

Canonical def-B skill-lifecycle handler for the ``dispatch_engine`` skill. The
single dispatch entrypoint is ``handle(request: ModelSkillRequest) ->
ModelSkillResult`` — the shared runtime binds it directly (Kafka auto-wiring's
``_make_dispatch_callback`` and RuntimeLocal's ``_resolve_handler_method`` both
resolve ``handle``), so the handler is executable, not merely registered
(OMN-14806, burning down OMN-14510's ``_missing_handle`` class).

``handle`` OWNS the behavior end-to-end: the ``ModelSkillRequest`` boundary is
enforced by the model's own field validators; a ``dry_run`` short-circuits without
routing; otherwise it routes through ``HandlerDispatchEngineRouter``, a thin router
that composes the two already-real pieces — RSD scoring (``node_rsd_fill_compute``)
and self-healing per-repo fan-out (``node_self_healing_dispatch_orchestrator``)
(OMN-13834). The skill-lifecycle boundary carries no ticket set (backlog polling is
owned upstream by ``node_pipeline_fill``), so a bare invocation with no candidates
resolves to ``no_candidates`` — an honest empty cycle, not a placeholder success.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_dispatch_router import (
    HandlerDispatchEngineRouter,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_dispatch_engine_request import (
    ModelDispatchEngineRequest,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_skill_request import (
    ModelSkillRequest,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_skill_result import (
    ModelSkillResult,
    SkillResultStatus,
)

__all__ = ["HandlerSkillRequested"]

logger = logging.getLogger(__name__)


class HandlerSkillRequested:
    """Canonical def-B skill-lifecycle handler for the dispatch_engine skill."""

    def __init__(
        self, event_bus: Any, router: HandlerDispatchEngineRouter | None = None
    ) -> None:
        self._event_bus = event_bus
        self._router = router or HandlerDispatchEngineRouter()

    async def handle(
        self, request: ModelSkillRequest | Mapping[str, object]
    ) -> ModelSkillResult:
        """Dispatch entrypoint: route one skill-lifecycle request to a result.

        Boundary validation is enforced by ``ModelSkillRequest`` (skill_name
        non-blank, skill_path ends with ``SKILL.md``). ``dry_run`` short-circuits
        to a ``dry_run`` result without routing; otherwise the request routes
        through the RSD + self-healing composition. The bare skill-lifecycle path
        carries no candidate ticket set, so the router resolves to
        ``no_candidates`` — an honest empty cycle.
        """
        if not isinstance(request, ModelSkillRequest):
            request = ModelSkillRequest.model_validate(dict(request))

        if request.dry_run:
            logger.debug(
                "dispatch_engine dry_run for skill=%r path=%r",
                request.skill_name,
                request.skill_path,
            )
            return ModelSkillResult(
                skill_name=request.skill_name,
                skill_path=request.skill_path,
                args=dict(request.args),
                status=SkillResultStatus.DRY_RUN,
            )

        receipt = await self._router.route(ModelDispatchEngineRequest(dry_run=False))
        logger.debug(
            "dispatch_engine routed skill=%r path=%r -> status=%s workers=%d",
            request.skill_name,
            request.skill_path,
            receipt.status.value,
            len(receipt.worker_specs),
        )
        return ModelSkillResult(
            skill_name=request.skill_name,
            skill_path=request.skill_path,
            args=dict(request.args),
            status=SkillResultStatus(receipt.status.value),
            run_id=receipt.run_id,
            total_selected=receipt.total_selected,
            worker_specs=receipt.worker_specs,
        )
