# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-facing tick handler of node_dev_roll_window_gate_effect (OMN-26501).

``HandlerDevRollWindowTick`` returns what one tick produced as a ``Sequence[BaseModel]``: the commands the
decision released, the decision, an abandoned-roll record, a held-age alarm. The runtime publishes a bare
sequence only when the multi-event (def-B fan-out) seam is on (``ONEX_MULTI_EVENT_PUBLISH_SEAM``, default
OFF); with it off the sequence is dropped with ``output_events=[]`` and status SUCCESS. Every
released command was marked released in the gate's own state and published to nobody, so the agent never saw
the roll and the gate held every later command until ``roll_completion_deadline_seconds`` abandoned it, then
released the next batch into the same drop: an hour of held commands per roll on dev.

The one return both runtimes publish, whatever the seam, is a single bare model (the runtime of omnibase_infra
publishes ``ModelHandlerOutput.events`` too, but the in-process dispatch of omnibase_core refuses that type, so
it cannot be the shared shape). This handler is the contract's tick handler: it runs the decision handler and hands
the runtime one event per tick, in the order the decision produced them.

The events of one tick wait in an outbox kept in the gate's durable state. The decision handler saves its own
state (held emptied, roll recorded) before it returns, and this handler saves the outbox straight after, so a
restart between ticks keeps the events still to publish; the next ticks drain the outbox before the decision
handler runs again. The agent folds the queued prefix of released commands into one roll as it did when they
arrived together.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.gate_context import (
    DevRollGateContext,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.handler_dev_roll_window_tick import (
    HandlerDevRollWindowTick,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)


class HandlerDevRollWindowTickPublish:
    """``handle(ModelRuntimeTick) -> None | BaseModel``: one event of the gate's tick, in a shape the runtime publishes."""

    def __init__(
        self,
        store: ProtocolDevRollGateStore | None = None,
        *,
        policy: ModelDevRollWindowGatePolicy | None = None,
        environ: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._context = DevRollGateContext(
            store, policy=policy, environ=environ, clock=clock
        )
        self._tick = HandlerDevRollWindowTick(
            store, policy=policy, environ=environ, clock=clock
        )

    def handle(self, request: ModelRuntimeTick) -> BaseModel | None:
        try:
            store = self._context.store()
        except NodeOverlayNotFoundError:
            return (
                None  # no gate overlay installed: nothing released, nothing to publish
            )
        state = store.load()
        if state.outbox:
            head, *waiting = state.outbox
            store.save(state.model_copy(update={"outbox": tuple(waiting)}))
            return head
        produced = tuple(self._tick.handle(request) or ())
        if not produced:
            return None
        first, *rest = produced
        if rest:  # a tick of one event (a hold) leaves nothing to queue: no write per idle tick
            store.save(store.load().model_copy(update={"outbox": tuple(rest)}))
        return first
