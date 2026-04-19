# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handler for `node_ledger_state_reducer` (OMN-8950 / OMN-9009).

REDUCER node. Pure: ``delta(state, event) -> (new_state, intents[])``.

Under the pure-reducer-as-effect architecture (epic OMN-9006), the reducer
emits a typed ``ModelPersistStateIntent`` carrying a ``ModelStateEnvelope``
populated from the newly computed state. The downstream effect node
``node_state_persist_effect`` consumes that intent and performs the actual
persistence side effect — the reducer itself does no I/O.

``handle()`` is the RuntimeLocal-protocol shim that wraps the delta tuple in
the dict convention ``{"state": ..., "intents": [...]}`` used by
``node_loop_state_reducer/handlers/handler_loop_state.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from omnibase_core.models.intents import ModelPersistStateIntent
from omnibase_core.models.state.model_state_envelope import ModelStateEnvelope

from omnimarket.events.ledger import ModelLedgerHashComputed
from omnimarket.nodes.node_ledger_state_reducer.models.model_ledger_state import (
    ModelLedgerState,
)

_NODE_ID = "ledger_state_reducer"


class HandlerLedgerStateReducer:
    """Reducer. Accumulates tick count, records last hash + line count."""

    def delta(
        self,
        state: ModelLedgerState,
        event: ModelLedgerHashComputed,
        *,
        emitted_at: datetime,
        intent_id: UUID,
    ) -> tuple[ModelLedgerState, list[ModelPersistStateIntent]]:
        """Pure FSM delta. No I/O, no env reads, no clock reads, no randomness.

        Emits a single ``ModelPersistStateIntent`` carrying the new state as
        envelope data for the downstream persist effect to write. The two
        non-determinism sources (``emitted_at`` timestamp and ``intent_id``
        UUID) are injected as keyword arguments so that replay and
        idempotence tests can pass identical ``(state, event)`` inputs and
        observe identical outputs.
        """
        new_state = ModelLedgerState(
            tick_count=state.tick_count + 1,
            last_hash=event.sha256_hex,
            last_line_count=event.line_count,
        )
        envelope = ModelStateEnvelope(
            node_id=_NODE_ID,
            data=new_state.model_dump(mode="json"),
            written_at=emitted_at,
        )
        intent = ModelPersistStateIntent(
            intent_id=intent_id,
            envelope=envelope,
            emitted_at=emitted_at,
            correlation_id=event.correlation_id,
        )
        return new_state, [intent]

    def handle(self, request: ModelLedgerHashComputed) -> dict[str, Any]:
        """RuntimeLocal-protocol shim. Wraps delta() tuple in dict convention.

        The shim is the effect boundary where clock + UUID generation happen.
        First invocation seeds from ``ModelLedgerState()`` default. Subsequent
        invocations would hydrate prior state from the persist effect's write
        path (out of scope here — the first delta is sufficient to prove the
        intent emission contract).
        """
        initial = ModelLedgerState()
        new_state, intents = self.delta(
            initial,
            request,
            emitted_at=datetime.now(UTC),
            intent_id=uuid4(),
        )
        return {
            "state": new_state.model_dump(mode="json"),
            "intents": [i.model_dump(mode="json") for i in intents],
        }
