# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completion and rejection handlers of node_dev_roll_gate_effect (OMN-24431).

The deploy agent answers every command it accepts with a completion or a rejection (a
command a newer one absorbed is rejected as superseded). Each answer for a released
command settles it; the roll is finished when none is outstanding, and then the batching
node's ``last_roll`` reads finished, healthy when a completion with a success status
arrived. If commands are held, the finish also asks for the next decision.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    ModelDeployRebuildCompleted,
    ModelDeployRebuildRejected,
)
from omnimarket.models.dev_roll import ModelDevRollFacts
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.handler_dev_roll_gate_base import (
    HandlerDevRollGateBase,
)
from omnimarket.nodes.node_dev_roll_gate_effect.handlers.logic_dev_roll_gate import (
    build_facts,
    settle_command,
    settle_timeout,
    with_request,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
)

Result = ModelDevRollFacts | ModelDevRollGateObserved


class _HandlerDevRollGateSettled(HandlerDevRollGateBase):
    def _settle(
        self,
        correlation_id: str,
        *,
        succeeded: bool,
        started_at: datetime | None,
        at: datetime | None,
    ) -> Result:
        try:
            policy = self._resolve_policy()
            store = self._resolve_store()
            now = self._clock()
            state = settle_timeout(store.load(), policy, now)
            state, ours, finished = settle_command(
                state,
                correlation_id,
                succeeded=succeeded,
                started_at=started_at,
                at=at if at is not None else now,
            )
            if not ours:
                store.save(state)
                return self._observed(
                    EnumDevRollGateObservedReason.NOT_OURS, correlation_id
                )
            if finished and state.held:
                facts = build_facts(state, policy, now, f"settled/{correlation_id}")
                store.save(with_request(state, policy, facts))
                return facts
            store.save(state)
        except (ValueError, OSError) as exc:
            return self._observed(
                EnumDevRollGateObservedReason.NODE_UNAVAILABLE,
                correlation_id,
                str(exc),
            )
        reason = (
            EnumDevRollGateObservedReason.ROLL_FINISHED
            if finished
            else EnumDevRollGateObservedReason.ROLL_PENDING
        )
        return self._observed(reason, correlation_id)


class HandlerDevRollGateCompleted(_HandlerDevRollGateSettled):
    """``handle(ModelDeployRebuildCompleted) -> facts | observed``."""

    def handle(self, request: ModelDeployRebuildCompleted) -> Result:
        return self._settle(
            request.correlation_id,
            succeeded=request.status is EnumRedeployStatus.SUCCESS,
            started_at=request.started_at,
            at=request.completed_at,
        )


class HandlerDevRollGateRejected(_HandlerDevRollGateSettled):
    """``handle(ModelDeployRebuildRejected) -> facts | observed``."""

    def handle(self, request: ModelDeployRebuildRejected) -> Result:
        return self._settle(
            str(request.correlation_id), succeeded=False, started_at=None, at=None
        )
