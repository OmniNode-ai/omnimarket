# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDispatchEngineRouter — thin router over the already-real dispatch pieces.

The dispatch_engine node used to be a facade: it logged ``"dispatch_engine
placeholder dispatch"`` and returned a hardcoded ``{"status": "dispatched"}``.
This router replaces that placeholder with a real routed dispatch composed from
two nodes that already do the work (OMN-13834):

  1. ``node_rsd_fill_compute`` (``HandlerRsdFill``) — pure RSD scoring / ranking
     of the candidate ticket set (backlog -> scored candidates).
  2. ``node_self_healing_dispatch_orchestrator``
     (``HandlerSelfHealingDispatchOrchestrator``) — deterministic per-repo
     grouping and (adapter-gated) worker fan-out.

Flow: ``candidate_tickets`` -> RSD rank + top_n/min_score cut -> self-healing
per-repo grouping -> ``ModelDispatchEngineReceipt`` with concrete worker specs.

Honest boundary: the receipt's ``worker_specs`` are the real per-repo dispatch
plan (RSD-scored, repo-grouped). Live agent launch (TeamCreate) is a runtime
side-effect owned by ``node_self_healing_dispatch_orchestrator``'s injected
dispatcher adapter. When a live dispatcher is injected, this router launches and
reports ``DISPATCHED``; without one it reports ``PLANNED`` (the plan) and leaves
launch to the runtime adapter. Backlog *polling* (Linear I/O) stays upstream in
``node_pipeline_fill`` — the router does not re-implement that I/O boundary.
"""

from __future__ import annotations

import logging

from omnimarket.events.self_healing_dispatch import (
    ModelDispatchGroup,
    ModelSelfHealingDispatchRequest,
)
from omnimarket.nodes.node_rsd_fill_compute.handlers.handler_rsd_fill import (
    HandlerRsdFill,
)
from omnimarket.nodes.node_self_healing_dispatch_orchestrator.handlers.handler_self_healing_dispatch_orchestrator import (
    HandlerSelfHealingDispatchOrchestrator,
    ProtocolSelfHealingDispatcher,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_dispatch_engine_receipt import (
    EnumDispatchEngineStatus,
    ModelDispatchEngineReceipt,
    ModelDispatchWorkerSpec,
)
from omnimarket.nodes.node_skill_dispatch_engine_orchestrator.models.model_dispatch_engine_request import (
    ModelDispatchEngineRequest,
)

logger = logging.getLogger(__name__)

__all__ = ["HandlerDispatchEngineRouter"]


class HandlerDispatchEngineRouter:
    """Routes a dispatch request through RSD scoring then self-healing fan-out."""

    def __init__(
        self,
        rsd_handler: HandlerRsdFill | None = None,
        self_healing_handler: HandlerSelfHealingDispatchOrchestrator | None = None,
        dispatcher: ProtocolSelfHealingDispatcher | None = None,
    ) -> None:
        self._rsd = rsd_handler or HandlerRsdFill()
        self._dispatcher = dispatcher
        # When a live dispatcher is injected, route the fan-out through a
        # self-healing orchestrator that owns it; otherwise operate in plan mode.
        self._self_healing = (
            self_healing_handler
            or HandlerSelfHealingDispatchOrchestrator(dispatcher=dispatcher)
        )

    async def route(
        self, request: ModelDispatchEngineRequest
    ) -> ModelDispatchEngineReceipt:
        """Execute one routed dispatch cycle and return a real receipt."""
        run_id = f"dispatch-engine-{request.correlation_id}"
        total_candidates = len(request.candidate_tickets)

        # 1. Score / rank via the RSD compute node (pure, deterministic).
        fill = await self._rsd.handle(
            correlation_id=request.correlation_id,
            scored_tickets=request.candidate_tickets,
            max_tickets=request.top_n,
        )
        selected = tuple(
            t for t in fill.selected_tickets if t.rsd_score >= request.min_score
        )

        if not selected:
            logger.info(
                "dispatch_engine: no candidates survived cuts "
                "(run_id=%s candidates=%d top_n=%d min_score=%.3f)",
                run_id,
                total_candidates,
                request.top_n,
                request.min_score,
            )
            return ModelDispatchEngineReceipt(
                run_id=run_id,
                correlation_id=request.correlation_id,
                status=EnumDispatchEngineStatus.NO_CANDIDATES,
                scored_candidates=(),
                worker_specs=(),
                total_candidates=total_candidates,
                total_selected=0,
                dry_run=request.dry_run,
            )

        # 2. Route survivors through the self-healing dispatch fan-out.
        #    Live launch happens only when a dispatcher adapter is injected AND
        #    this is not a dry run; otherwise we run the grouper in plan mode.
        live = self._dispatcher is not None and not request.dry_run
        sh_request = ModelSelfHealingDispatchRequest(
            ticket_ids=tuple(t.ticket_id for t in selected),
            repo_hints=request.repo_hints,
            run_id=run_id,
            dry_run=not live,
        )
        sh_result = self._self_healing.handle(sh_request)

        worker_specs = tuple(
            self._to_worker_spec(group, run_id) for group in sh_result.dispatch_groups
        )

        if request.dry_run:
            status = EnumDispatchEngineStatus.DRY_RUN
        elif live:
            status = EnumDispatchEngineStatus.DISPATCHED
        else:
            status = EnumDispatchEngineStatus.PLANNED

        logger.info(
            "dispatch_engine: routed %d/%d candidates into %d worker spec(s) "
            "(run_id=%s status=%s)",
            len(selected),
            total_candidates,
            len(worker_specs),
            run_id,
            status.value,
        )

        return ModelDispatchEngineReceipt(
            run_id=run_id,
            correlation_id=request.correlation_id,
            status=status,
            scored_candidates=selected,
            worker_specs=worker_specs,
            total_candidates=total_candidates,
            total_selected=len(selected),
            dry_run=request.dry_run,
        )

    @staticmethod
    def _to_worker_spec(
        group: ModelDispatchGroup, run_id: str
    ) -> ModelDispatchWorkerSpec:
        """Derive a concrete worker spec from a self-healing dispatch group.

        Self-healing sets ``worker_name`` only when a live dispatcher launches the
        group (plan/dry-run leaves it as ``dry-run-<repo>`` or empty). The router
        always mints a deterministic, non-placeholder worker name so the receipt
        carries real worker specs regardless of launch mode.
        """
        worker_name = group.worker_name
        if not worker_name or worker_name.startswith("dry-run-"):
            worker_name = f"{run_id}-{group.repo}"
        return ModelDispatchWorkerSpec(
            worker_name=worker_name,
            repo=group.repo,
            ticket_ids=group.ticket_ids,
        )
