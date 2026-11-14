# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Hold handler of node_dev_roll_gate_effect (OMN-24431).

One forwarded dev deploy command in: it is stored durably and the batching node gets
one decision request (``ModelDevRollFacts``) naming everything held, or the gate says
why it did not. With the overlay's mode ``off`` the command is not held: it comes back
as a release of one, so nothing handed to the gate is ever stranded. Never raises out
of ``handle`` for an overlay or store problem: that is a ``node_unavailable`` answer
and the command stays on the bus for redelivery.
"""

from __future__ import annotations

from uuid import uuid5

from omnimarket.models.dev_roll import (
    ModelDevRollFacts,
    ModelDevRollHoldRequest,
    ModelDevRollReleased,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.handler_dev_roll_gate_base import (
    HandlerDevRollGateBase,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.logic_dev_roll_gate import (
    NAMESPACE,
    build_facts,
    hold,
    settle_timeout,
    with_request,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
)

Result = ModelDevRollFacts | ModelDevRollReleased | ModelDevRollGateObserved


class HandlerDevRollGateHold(HandlerDevRollGateBase):
    """``handle(ModelDevRollHoldRequest) -> facts | release | observed``."""

    def handle(self, request: ModelDevRollHoldRequest) -> Result:
        command = request.command
        key = str(command.correlation_id)
        try:
            policy = self._resolve_policy()
            store = self._resolve_store()
            now = self._clock()
            state = settle_timeout(store.load(), policy, now)
            if policy.mode == "off":
                return ModelDevRollReleased(
                    release_id=uuid5(NAMESPACE, f"passthrough/{key}"),
                    decision_correlation_id=None,
                    released_at=now,
                    commands=(command,),
                )
            if key in state.seen_command_ids:
                return self._observed(
                    EnumDevRollGateObservedReason.ALREADY_HELD,
                    key,
                    "this correlation id was already held or released",
                )
            state = hold(state, policy, command, now)
            facts = build_facts(state, policy, now, f"hold/{key}")
            store.save(with_request(state, policy, facts))
        except (ValueError, OSError) as exc:
            return self._observed(
                EnumDevRollGateObservedReason.NODE_UNAVAILABLE, key, str(exc)
            )
        return facts
