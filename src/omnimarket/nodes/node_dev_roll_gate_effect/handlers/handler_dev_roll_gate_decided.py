# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Decided handler of node_dev_roll_gate_effect (OMN-24431).

One decision of node_dev_roll_request_compute in. Only a ``roll_now`` that answers a
request this gate published releases anything, and only the held commands that decision
rolled (its ``rolled_event_ids``, plus any event it refused, which would otherwise stay
held for ever), oldest first, in one :class:`ModelDevRollReleased`. A command held after
the decision's facts stays held: its own window has not ended. No release while the
previous batch has an outstanding command: the decision was made on facts that may be
stale, and the gate checks the roll's state again here. The decision's ``shadow`` flag is
not read: the batching node only decides, and the gate's mode is its own overlay's.
"""

from __future__ import annotations

from uuid import UUID, uuid5

from omnimarket.models.dev_roll import (
    EnumDevRollAction,
    ModelDevRollDecided,
    ModelDevRollReleased,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.handler_dev_roll_gate_base import (
    HandlerDevRollGateBase,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.logic_dev_roll_gate import (
    NAMESPACE,
    release,
    settle_timeout,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
)

Result = ModelDevRollReleased | ModelDevRollGateObserved


class HandlerDevRollGateDecided(HandlerDevRollGateBase):
    """``handle(ModelDevRollDecided) -> release | observed``."""

    def handle(self, request: ModelDevRollDecided) -> Result:
        key = str(request.correlation_id)
        try:
            policy = self._resolve_policy()
            store = self._resolve_store()
            now = self._clock()
            state = settle_timeout(store.load(), policy, now)
            if key not in state.requested_correlation_ids:
                return self._observed(EnumDevRollGateObservedReason.NOT_OURS, key)
            if policy.mode != "live":
                return self._observed(EnumDevRollGateObservedReason.NOT_LIVE, key)
            if request.action is not EnumDevRollAction.ROLL_NOW:
                reason = request.hold_reason.value if request.hold_reason else None
                store.save(state)
                return self._observed(
                    EnumDevRollGateObservedReason.HELD_BY_DECISION, key, reason
                )
            if state.outstanding:
                store.save(state)
                return self._observed(
                    EnumDevRollGateObservedReason.ROLL_IN_PROGRESS,
                    key,
                    f"{len(state.outstanding)} released command(s) have no terminal event",
                )
            rolled: set[UUID] = set(request.rolled_event_ids) | {
                refused.event_id for refused in request.refused_events
            }
            chosen = tuple(
                sorted(
                    (h for h in state.held if h.command.correlation_id in rolled),
                    key=lambda h: h.held_at,
                )
            )
            if not chosen:
                store.save(state)
                return self._observed(EnumDevRollGateObservedReason.NOTHING_HELD, key)
            release_id = uuid5(NAMESPACE, f"release/{key}")
            git_ref = (
                request.would_request.git_ref
                if request.would_request is not None
                else ""
            )
            store.save(release(state, chosen, git_ref, release_id, now))
        except (ValueError, OSError) as exc:
            return self._observed(
                EnumDevRollGateObservedReason.NODE_UNAVAILABLE, key, str(exc)
            )
        return ModelDevRollReleased(
            release_id=release_id,
            decision_correlation_id=request.correlation_id,
            released_at=now,
            commands=tuple(h.command for h in chosen),
        )
