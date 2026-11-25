# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostHygieneRunRequested: the on-demand route of the host hygiene orchestrator (OMN-24610).

handle takes the caller's run-requested command and runs the same shared body as a runtime tick:
it reads the overlay, asks the decision compute which cleanups are due on which hosts, sends one
lab work unit per due cleanup to its host, and returns the run event with every decision and every
unit's typed result. The event's correlation id is the command's, so ``onex run-node`` matches the
answer to the caller's request. The route neither consults nor moves the schedule gate: an
on-demand run neither delays nor advances the next scheduled tick, and no schedule run event is
emitted. A missing or invalid overlay is answered with a refused run naming the cause (a missing one is
also logged once).
"""

from __future__ import annotations

import datetime as dt

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    NodeOverlayNotFoundError,
)
from omnimarket.models.model_host_hygiene_orchestrator import (
    EnumHostHygieneRunOutcome,
    EnumHostHygieneRunTrigger,
    ModelHostHygieneRunCompleted,
)
from omnimarket.models.model_host_hygiene_run_requested import (
    ModelHostHygieneRunRequested,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    run_completed_topic,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_hygiene_run import (
    NODE_NAME,
    HostHygieneRunBase,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.protocols import (
    ProtocolHostHygieneLabWorkSender,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.sender_factory import (
    build_sender,
)


class HandlerHostHygieneRunRequested(HostHygieneRunBase):
    """ORCHESTRATOR handler: a run-requested command in, the hygiene run event out."""

    HANDLER_ID = f"{NODE_NAME}.host_hygiene.run_requested"
    SHARES_PENDING_RUNS = False

    async def handle(
        self, request: ModelHostHygieneRunRequested
    ) -> ModelHandlerOutput[None]:
        correlation_id = request.correlation_id
        try:
            overlay = self._overlay()
        except NodeOverlayError as exc:
            # A missing overlay and an invalid one (its text names fields, never values) are both
            # answered with a refused run: a caller that gets no answer can only time out.
            if isinstance(exc, NodeOverlayNotFoundError):
                self._refusals.refuse(exc)
            now = self._clock().astimezone(dt.UTC)
            run = ModelHostHygieneRunCompleted(
                run_id=f"{NODE_NAME}-{correlation_id}",
                job_id="",
                tick_id="",
                started_at=now,
                finished_at=self._clock(),
                outcome=EnumHostHygieneRunOutcome.REFUSED,
                decided=(),
                units=(),
                trigger=EnumHostHygieneRunTrigger.ON_DEMAND,
                detail=str(exc)[:2000],
            )
        else:
            self._refusals.recovered()
            now = self._clock().astimezone(dt.UTC)
            sender: ProtocolHostHygieneLabWorkSender = (
                self._sender if self._sender is not None else build_sender(overlay)
            )
            run = await self._run(
                overlay=overlay,
                now=now,
                sender=sender,
                run_id=f"{overlay.job_id}-{correlation_id}",
                tick_id="",
                trigger=EnumHostHygieneRunTrigger.ON_DEMAND,
            )
        envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
            payload=run,
            correlation_id=correlation_id,
            event_type=run_completed_topic(),
        )
        return self._output(correlation_id, (envelope,))


__all__: list[str] = ["HandlerHostHygieneRunRequested"]
