# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for `node_ledger_state_reducer` (OMN-8950).

REDUCER node. Pure: `delta(state, event) -> (new_state, intents[])`.
`handle()` is a RuntimeLocal-protocol shim that wraps the tuple in the
dict convention `{"state": ..., "intents": ...}` matching
`node_loop_state_reducer/handlers/handler_loop_state.py:96-110`.

RuntimeLocal's sink path (OMN-8946) constructs the ModelStateEnvelope
in its own path; this handler itself does NOT return an envelope.
"""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_ledger_hash_compute.models.model_ledger_hash_computed import (
    ModelLedgerHashComputed,
)
from omnimarket.nodes.node_ledger_state_reducer.models.model_ledger_state import (
    ModelLedgerState,
)


class HandlerLedgerStateReducer:
    """Reducer. Accumulates tick count, records last hash + line count."""

    def delta(
        self,
        state: ModelLedgerState,
        event: ModelLedgerHashComputed,
    ) -> tuple[ModelLedgerState, list[dict[str, Any]]]:
        """Pure FSM delta. No I/O, no env reads, no bus publishes."""
        new_state = ModelLedgerState(
            tick_count=state.tick_count + 1,
            last_hash=event.sha256_hex,
            last_line_count=event.line_count,
        )
        return new_state, []

    def handle(self, request: ModelLedgerHashComputed) -> dict[str, Any]:
        """RuntimeLocal-protocol shim. Wraps delta() tuple in dict convention.

        First invocation seeds from ModelLedgerState() default. Subsequent
        invocations would need to read prior state from ProtocolStateStore
        (deferred — out of scope for this didactic demo; the first delta is
        sufficient to prove the sink wiring).
        """
        initial = ModelLedgerState()
        new_state, intents = self.delta(initial, request)
        return {
            "state": new_state.model_dump(mode="json"),
            "intents": intents,
        }
