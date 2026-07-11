# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for `node_ledger_orchestrator` (OMN-8947).

Canonical thin shape (OMN-14242): ``handle()`` takes a single typed payload
and returns its single typed emit directly — no ``ModelHandlerOutput``
envelope, no coercion in the handler. The runtime wraps the return value
into an envelope/event publish; this handler is a pure single-emit mapping.
"""

from __future__ import annotations

from omnimarket.nodes.node_ledger_orchestrator.models.model_ledger_tick_command import (
    ModelLedgerAppendCommand,
    ModelLedgerTickCommand,
)


class HandlerLedgerOrchestrator:
    """Orchestrator shell. Receives a tick command, emits an append command event."""

    def handle(self, payload: ModelLedgerTickCommand) -> ModelLedgerAppendCommand:
        """Convert tick → append command.

        Single emit: the append-effect node consumes the returned
        `ModelLedgerAppendCommand` in the next link of the four-node chain.
        `correlation_id` is carried forward from the payload.
        """
        return ModelLedgerAppendCommand(
            tick_id=payload.tick_id,
            correlation_id=payload.correlation_id,
        )
