# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick handler of node_dev_roll_gate_effect (OMN-24431).

The batching node reads no clock, so a window that ends with no new change needs a
fresh decision request. One tick in: when commands are held the batching node gets a
decision request at the tick's time; in mode ``off`` whatever is held is released at
once, so switching the gate off never strands a command.
"""

from __future__ import annotations

from uuid import uuid5

from omnimarket.models.dev_roll import ModelDevRollFacts, ModelDevRollReleased
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.handler_dev_roll_gate_base import (
    HandlerDevRollGateBase,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.logic_dev_roll_gate import (
    NAMESPACE,
    build_facts,
    settle_timeout,
    with_request,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
    ModelDevRollGateTick,
)

Result = ModelDevRollFacts | ModelDevRollReleased | ModelDevRollGateObserved


class HandlerDevRollGateTick(HandlerDevRollGateBase):
    """``handle(ModelDevRollGateTick) -> facts | release | observed``."""

    def handle(self, request: ModelDevRollGateTick) -> Result:
        cause = f"tick/{request.tick_id}"
        try:
            policy = self._resolve_policy()
            store = self._resolve_store()
            now = self._clock()
            state = settle_timeout(store.load(), policy, now)
            if not state.held:
                store.save(state)
                return self._observed(
                    EnumDevRollGateObservedReason.NOTHING_HELD, str(request.tick_id)
                )
            if policy.mode == "off":
                chosen = tuple(
                    sorted(
                        state.held,
                        key=lambda h: h.held_at,
                    )
                )
                release_id = uuid5(NAMESPACE, f"drain/{cause}/{now.isoformat()}")
                store.save(state.model_copy(update={"held": ()}))
                return ModelDevRollReleased(
                    release_id=release_id,
                    decision_correlation_id=None,
                    released_at=now,
                    commands=tuple(h.command for h in chosen),
                )
            facts = build_facts(state, policy, now, cause)
            store.save(with_request(state, policy, facts))
        except (ValueError, OSError) as exc:
            return self._observed(
                EnumDevRollGateObservedReason.NODE_UNAVAILABLE,
                str(request.tick_id),
                str(exc),
            )
        return facts
