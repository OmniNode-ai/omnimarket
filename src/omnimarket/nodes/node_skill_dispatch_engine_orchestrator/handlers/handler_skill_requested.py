# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill dispatch handler for node_skill_dispatch_engine_orchestrator.

This is the skill-lifecycle entry for the ``dispatch_engine`` skill. It no longer
returns a ``"dispatched"`` placeholder (OMN-13834): the live path routes through
``HandlerDispatchEngineRouter``, a thin router that composes the two already-real
pieces — RSD scoring (``node_rsd_fill_compute``) and self-healing per-repo fan-out
(``node_self_healing_dispatch_orchestrator``).

Two surfaces:
    * ``dispatch(request)`` — the real routed dispatch over a candidate ticket set,
      returning a ``ModelDispatchEngineReceipt`` with concrete worker specs.
    * ``handle_skill_requested(...)`` — the skill-lifecycle shim invoked over the
      command bus. It validates the ``ModelSkillRequest`` boundary and, on the
      live path, routes. The shim carries no ticket set (backlog polling is owned
      upstream by ``node_pipeline_fill``), so a bare skill invocation with no
      candidates resolves to ``no_candidates`` — an honest empty cycle, not a
      placeholder success.
"""

from __future__ import annotations

import logging
from typing import Any

from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.handlers.handler_dispatch_router import (
    HandlerDispatchEngineRouter,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_dispatch_engine_receipt import (
    ModelDispatchEngineReceipt,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_dispatch_engine_request import (
    ModelDispatchEngineRequest,
)

__all__ = ["HandlerSkillRequested"]

logger = logging.getLogger(__name__)


class HandlerSkillRequested:
    """Skill-lifecycle handler for the dispatch_engine skill request event."""

    def __init__(
        self, event_bus: Any, router: HandlerDispatchEngineRouter | None = None
    ) -> None:
        self._event_bus = event_bus
        self._router = router or HandlerDispatchEngineRouter()

    async def dispatch(
        self, request: ModelDispatchEngineRequest
    ) -> ModelDispatchEngineReceipt:
        """Run one real routed dispatch and return the receipt."""
        return await self._router.route(request)

    async def handle_skill_requested(
        self,
        *,
        skill_name: str,
        skill_path: str,
        args: dict[str, str] | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Handle a skill-requested event over the command bus.

        Validates the request boundary, then routes. ``dry_run`` short-circuits to
        a ``dry_run`` status without routing. The live path routes through the RSD
        + self-healing composition; a bare shim invocation with no backlog access
        resolves to ``no_candidates``.
        """
        args = args or {}
        if not skill_name or not skill_name.strip():
            raise ValueError("skill_name must not be blank")
        if not skill_path or not skill_path.endswith("SKILL.md"):
            raise ValueError("skill_path must end with 'SKILL.md'")

        base: dict[str, Any] = {
            "skill_name": skill_name,
            "skill_path": skill_path,
            "args": dict(args),
        }

        if dry_run:
            logger.debug(
                "dispatch_engine dry_run for skill=%r path=%r", skill_name, skill_path
            )
            return {**base, "status": "dry_run"}

        receipt = await self._router.route(ModelDispatchEngineRequest(dry_run=False))
        logger.debug(
            "dispatch_engine routed skill=%r path=%r -> status=%s workers=%d",
            skill_name,
            skill_path,
            receipt.status.value,
            len(receipt.worker_specs),
        )
        return {
            **base,
            "status": receipt.status.value,
            "run_id": receipt.run_id,
            "total_selected": receipt.total_selected,
            "worker_specs": [
                {
                    "worker_name": spec.worker_name,
                    "repo": spec.repo,
                    "ticket_ids": list(spec.ticket_ids),
                }
                for spec in receipt.worker_specs
            ],
        }
