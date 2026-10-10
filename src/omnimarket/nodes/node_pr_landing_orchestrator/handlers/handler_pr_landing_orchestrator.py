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

OMN-20866: by default the config is the contract's ``landing_config`` block
(:mod:`...orchestration.contract_config`) and the head-check classifier is
node_pr_lifecycle_triage_compute's ``classify_head_checks``
(:mod:`...orchestration.head_checks`).

OMN-20127: the compose runtime calls :meth:`HandlerPrLandingOrchestrator.handle_async`
(it prefers that entrypoint when the class declares it) and publishes the events
of the ``ModelHandlerOutput`` it returns. A bare ``list`` from ``handle`` is a
def-B fan-out sequence, which omnibase_infra ``handler_wiring`` drops while the
fan-out seam flag is off, so without this entrypoint the state_io outbox captured
nothing and every row waited on a GitHub read that was never sent.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID, uuid4, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
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
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.contract_config import (
    load_contract_config,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.core import (
    PrLandingOrchestratorConfig,
    PrLandingOrchestratorPorts,
    PrLandingStepResult,
    run_leg,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.head_checks import (
    TriageHeadCheckClassifier,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.outbox import (
    PR_LANDING_NAMESPACE,
)
from omnimarket.nodes.node_pr_landing_orchestrator.orchestration.ports import (
    LazyPrLandingReducer,
    ProtocolPrLandingArmGate,
    ProtocolPrLandingHeadCheckClassifier,
    ProtocolPrLandingReducer,
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
    ) -> None:
        self._ports = PrLandingOrchestratorPorts(
            reducer=reducer if reducer is not None else LazyPrLandingReducer(),
            arm_gate=arm_gate if arm_gate is not None else HandlerPrArmGate(),
            classifier=classifier
            if classifier is not None
            else TriageHeadCheckClassifier(),
        )
        # OMN-20866: the contract's landing_config, not a code default, decides
        # each repository's mode; an explicit config is for tests only.
        self._config = config if config is not None else load_contract_config()
        self._local_store: ProtocolPrLandingRowStore = (
            store if store is not None else InMemoryPrLandingRowStore()
        )

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

    async def handle(self, request: PrLandingOrchestratorInput) -> list[BaseModel]:
        """Apply one message to its PR's landing row; return what to publish."""
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

    async def handle_async(
        self, request: PrLandingOrchestratorInput
    ) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: the leg's emissions as publishable handler output.

        The compose runtime publishes ``events`` through the contract's
        ``published_events`` map and, for this ``state_io`` node, captures them
        into the row's outbox in the same compare-and-set (OMN-20127).
        """
        events = await self.handle(request)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=_correlation_of(request),
            handler_id="node_pr_landing_orchestrator.workflow",
            events=tuple(events),
        )


def _correlation_of(request: PrLandingOrchestratorInput) -> UUID:
    """The inbound correlation id, or one derived from the landing key."""
    candidate = getattr(request, "correlation_id", None)
    if isinstance(candidate, UUID):
        return candidate
    if isinstance(candidate, str):
        try:
            return UUID(candidate)
        except ValueError:
            pass
    return uuid5(PR_LANDING_NAMESPACE, f"{request.landing_key}|handler-output")


__all__: list[str] = ["HandlerPrLandingOrchestrator"]
