# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for `node_ledger_orchestrator` (OMN-8947).

Per ONEX rules: ORCHESTRATOR emits events/intents, never returns a result.
"""

from __future__ import annotations

from uuid import uuid4

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.nodes.node_ledger_orchestrator.models.model_ledger_tick_command import (
    ModelLedgerAppendCommand,
    ModelLedgerTickCommand,
)


class HandlerLedgerOrchestrator:
    """Orchestrator shell. Receives a tick command, emits an append command event."""

    def handle(self, request: ModelLedgerTickCommand) -> ModelHandlerOutput[None]:
        """Convert tick → append command.

        Emits a single `ModelLedgerAppendCommand` event; the append-effect node
        consumes it in the next link of the four-node chain.

        `input_envelope_id` is synthesized per-invocation; production runtime
        would pass the envelope ID of the triggering message. `correlation_id`
        is carried forward from the request.
        """
        append_cmd = ModelLedgerAppendCommand(
            tick_id=request.tick_id,
            correlation_id=request.correlation_id,
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=uuid4(),
            correlation_id=request.correlation_id,
            handler_id="node_ledger_orchestrator",
            events=(append_cmd,),
        )
