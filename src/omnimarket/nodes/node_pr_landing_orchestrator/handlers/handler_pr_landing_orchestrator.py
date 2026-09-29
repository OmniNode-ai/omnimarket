# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrLandingOrchestrator: the PR landing workflow's orchestrator (OMN-19829).

The delegation pattern (node_delegation_orchestrator): one handler instance
serves every route of the contract; each consumed message is one leg against
the ``state_io`` row keyed ``landing_key`` in table ``pr_landing_workflow_state``;
the leg's typed emissions are published by class name through the contract's
``published_events`` (the runtime's in-row outbox publishes them from the
committed row, at least once).

The leg logic is :func:`...orchestration.core.run_leg`. This class only binds it
to a row store under compare-and-set with retry: the runtime's ``state_io``
seam when a dispatch has bound its rows, the in-memory store otherwise.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Literal

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.nodes.node_pr_arm_gate_compute.handlers.handler_arm_gate import (
    HandlerPrArmGate,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_ingress import (
    PrLandingOrchestratorInput,
)
from omnimarket.nodes.node_pr_landing_orchestrator.models.model_pr_landing_workflow_row import (
    ModelPrLandingWorkflowRow,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
    PrLandingOrchestratorPorts,
    PrLandingStepResult,
    run_leg,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    LazyPrLandingReducer,
    ProtocolPrLandingArmGate,
    ProtocolPrLandingHeadCheckClassifier,
    ProtocolPrLandingReducer,
    UnwiredHeadCheckClassifier,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.reconcile_tick import (
    ProtocolPrLandingRowLister,
    UnwiredPrLandingRowLister,
    build_reconcile_commands,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.row_store import (
    InMemoryPrLandingRowStore,
    ProtocolPrLandingRowStore,
    read_active_state_io_rows,
    run_with_cas_retry,
)
from omnimarket.nodes.node_pr_landing_orchestrator.state_codec import (
    shared_state_io_store,
)

logger = logging.getLogger(__name__)


class HandlerPrLandingOrchestrator:
    """One leg per consumed message, under the row's compare-and-set."""

    def __init__(
        self,
        *,
        reducer: ProtocolPrLandingReducer | None = None,
        arm_gate: ProtocolPrLandingArmGate | None = None,
        classifier: ProtocolPrLandingHeadCheckClassifier | None = None,
        config: PrLandingOrchestratorConfig | None = None,
        store: ProtocolPrLandingRowStore | None = None,
        row_lister: ProtocolPrLandingRowLister | None = None,
    ) -> None:
        self._ports = PrLandingOrchestratorPorts(
            reducer=reducer if reducer is not None else LazyPrLandingReducer(),
            arm_gate=arm_gate if arm_gate is not None else HandlerPrArmGate(),
            classifier=classifier
            if classifier is not None
            else UnwiredHeadCheckClassifier(),
        )
        self._config = config if config is not None else PrLandingOrchestratorConfig()
        self._local_store: ProtocolPrLandingRowStore = (
            store if store is not None else InMemoryPrLandingRowStore()
        )
        # OMN-19829 (T7 deferred item #4): unwired by default, matching
        # ``classifier`` above -- production listing needs the
        # pr_landing_workflow_state table (T7 deferred item #3), which does
        # not exist yet.
        self._row_lister: ProtocolPrLandingRowLister = (
            row_lister if row_lister is not None else UnwiredPrLandingRowLister()
        )
        # In-process elapsed-time gate for the reconciliation-tick fan-out,
        # the same idiom HandlerDeadLetterPrune uses for its own schedule:
        # ticks arrive far more often than the configured interval.
        self._last_reconcile_tick: datetime | None = None

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    def _store(self) -> ProtocolPrLandingRowStore:
        if read_active_state_io_rows() is not None:
            return shared_state_io_store()
        return self._local_store

    async def handle(
        self, request: PrLandingOrchestratorInput | ModelRuntimeTick
    ) -> list[BaseModel]:
        """Apply one message to its PR's landing row; return what to publish.

        A ``ModelRuntimeTick`` is not keyed to one row: it fans out to one
        ``ModelPrLandingReconcileCommand`` per non-terminal row instead of
        running a leg (OMN-19829, T7 deferred item #4).
        """
        if isinstance(request, ModelRuntimeTick):
            return list(await self._handle_reconcile_tick(request))
        key = request.landing_key

        async def decide(
            row: ModelPrLandingWorkflowRow | None,
        ) -> tuple[ModelPrLandingWorkflowRow | None, PrLandingStepResult]:
            result = await run_leg(row, request, config=self._config, ports=self._ports)
            return result.row, result

        result = await run_with_cas_retry(self._store(), key, decide)
        if result.dropped_reason is not None:
            logger.info(
                "[PR-LANDING] %s %s dropped: %s",
                key,
                type(request).__name__,
                result.dropped_reason,
            )
        return list(result.emitted)

    async def _handle_reconcile_tick(
        self, tick: ModelRuntimeTick
    ) -> tuple[BaseModel, ...]:
        """List every non-terminal row and build one reconcile command each.

        Gated on ``config.reconcile_tick_interval_seconds`` elapsed since the
        last scheduled run, using an in-process last-run timestamp -- the
        same idiom ``node_dead_letter_prune_effect`` uses for
        ``schedule.run_interval_seconds`` and ``node_github_pr_poller_effect``
        for ``poll_interval_seconds``. Updates ``self._last_reconcile_tick``
        only when the run proceeds, so a skipped tick never resets the
        interval.
        """
        now = tick.now
        interval = timedelta(seconds=self._config.reconcile_tick_interval_seconds)
        last = self._last_reconcile_tick
        if last is not None and (now - last) < interval:
            logger.debug(
                "[PR-LANDING] reconcile tick %s skipped: "
                "reconcile_tick_interval_seconds (%ss) has not elapsed",
                tick.tick_id,
                self._config.reconcile_tick_interval_seconds,
            )
            return ()
        self._last_reconcile_tick = now
        rows = await self._row_lister.list_non_terminal()
        return build_reconcile_commands(rows, now=now, tick_id=str(tick.tick_id))


__all__: list[str] = ["HandlerPrLandingOrchestrator"]
