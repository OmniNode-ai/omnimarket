# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPrHandoffOrchestrator: the PR handoff workflow's orchestrator (OMN-20636).

One handler instance serves every route of the contract; each consumed message
(a lane's request, a PR watcher observation, the ledger effect's answer) is one
leg against the ``state_io`` row keyed ``handoff_key`` (repo#n) in table
``pr_handoff_workflow_state``. The leg is :func:`...orchestration.core.run_leg`;
this class binds it to a row store under compare-and-set with retry: the
runtime's ``state_io`` seam when a dispatch has bound its rows, the in-memory
store otherwise. The leg's typed emissions are published by class name through
the contract's ``published_events``.
"""

from __future__ import annotations

import logging
from typing import Literal
from uuid import UUID, uuid4, uuid5

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.models.pr_handoff import (
    ModelPrHandoffAccepted,
    ModelPrHandoffFailed,
    ModelPrHandoffHandedOff,
    ModelPrHandoffLedgerAppendCommand,
)
from omnimarket.nodes.node_pr_handoff_decision_compute.handlers.handler_pr_handoff_decision import (
    HandlerPrHandoffDecision,
    invalid_request_reason,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.models.model_pr_handoff_workflow_row import (
    ModelPrHandoffWorkflowRow,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.core import (
    PR_HANDOFF_NAMESPACE,
    NoLedgerHolds,
    PrHandoffMessage,
    PrHandoffPorts,
    PrHandoffStepResult,
    ProtocolPrHandoffDecider,
    ProtocolPrHandoffHoldReader,
    run_leg,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.orchestration.row_store import (
    ProtocolPrHandoffRowStore,
    read_active_state_io_rows,
    run_with_cas_retry,
)
from omnimarket.nodes.node_pr_handoff_orchestrator.state_codec import (
    process_row_store,
    shared_state_io_store,
)

logger = logging.getLogger(__name__)


class HandlerPrHandoffOrchestrator:
    """One leg per consumed message, under the row's compare-and-set."""

    def __init__(
        self,
        *,
        decider: ProtocolPrHandoffDecider | None = None,
        holds: ProtocolPrHandoffHoldReader | None = None,
        store: ProtocolPrHandoffRowStore | None = None,
    ) -> None:
        self._ports = PrHandoffPorts(
            decider=decider if decider is not None else HandlerPrHandoffDecision(),
            holds=holds if holds is not None else NoLedgerHolds(),
        )
        self._local_store: ProtocolPrHandoffRowStore = (
            store if store is not None else process_row_store()
        )

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    def _store(self) -> ProtocolPrHandoffRowStore:
        if read_active_state_io_rows() is not None:
            return shared_state_io_store()
        return self._local_store

    async def handle(
        self, request: PrHandoffMessage
    ) -> list[
        ModelPrHandoffAccepted
        | ModelPrHandoffHandedOff
        | ModelPrHandoffFailed
        | ModelPrHandoffLedgerAppendCommand
    ]:
        """Apply one message to its PR's handoff row; return what to publish, in order."""
        key = request.handoff_key

        async def decide(
            row: ModelPrHandoffWorkflowRow | None,
        ) -> tuple[ModelPrHandoffWorkflowRow | None, PrHandoffStepResult]:
            result = run_leg(
                row, request, ports=self._ports, invalid_reason=invalid_request_reason
            )
            return result.row, result

        result = await run_with_cas_retry(self._store(), key, decide)
        if result.dropped_reason is not None:
            logger.info(
                "[PR-HANDOFF] %s %s dropped: %s",
                key,
                type(request).__name__,
                result.dropped_reason,
            )
        else:
            episode = result.row.episode if result.row is not None else None
            logger.info(
                "[PR-HANDOFF] %s %s -> %s emitted=%s",
                key,
                type(request).__name__,
                episode.state.value if episode is not None else "no request",
                [type(e).__name__ for e in result.emitted],
            )
        return list(result.emitted)

    async def handle_async(self, request: PrHandoffMessage) -> ModelHandlerOutput[None]:
        """Runtime entrypoint: the leg's emissions as publishable handler output.

        The compose runtime publishes ``events`` through the contract's
        ``published_events`` map and, for this ``state_io`` node, captures them
        into the row's outbox in the same compare-and-set (OMN-20127).
        """
        events = await self.handle(request)
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=_correlation_of(request),
            handler_id="node_pr_handoff_orchestrator.workflow",
            events=tuple(events),
        )


def _correlation_of(request: PrHandoffMessage) -> UUID:
    """The request's correlation id, or one derived from the handoff key."""
    candidate = getattr(request, "correlation_id", None)
    if isinstance(candidate, UUID):
        return candidate
    return uuid5(PR_HANDOFF_NAMESPACE, f"{request.handoff_key}|handler-output")


__all__: list[str] = ["HandlerPrHandoffOrchestrator"]
