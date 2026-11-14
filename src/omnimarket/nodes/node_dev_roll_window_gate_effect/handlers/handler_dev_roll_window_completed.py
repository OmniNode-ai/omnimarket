# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completed handler of node_dev_roll_window_gate_effect (OMN-19998).

The deploy agent's completion of a command the gate released ends the roll the gate
started: it records the finish and the health, which the batching decision reads to
hold the next release until the previous roll has finished and has been healthy for the
overlay's bound. A completion for any other command changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    ModelDeployRebuildCompleted,
)
from omnimarket.models.dev_roll import EnumDevRollHealth, ModelDevRollRecord
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.gate_context import (
    DevRollGateContext,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateRollRecorded,
    ModelDevRollWindowGatePolicy,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.protocols import (
    ProtocolDevRollGateStore,
)


class HandlerDevRollWindowCompleted:
    """``handle(ModelDeployRebuildCompleted) -> ModelDevRollGateRollRecorded | None``."""

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

    def handle(
        self, request: ModelDeployRebuildCompleted
    ) -> ModelDevRollGateRollRecorded | None:
        store = self._context.store()
        state = store.load()
        last = state.last_roll
        if last is None or request.correlation_id not in state.released_ids:
            return None
        if last.finished_at is not None:
            return None
        now = self._context.now()
        finished = request.completed_at or now
        healthy = request.status is EnumRedeployStatus.SUCCESS
        store.save(
            state.model_copy(
                update={
                    "last_roll": ModelDevRollRecord(
                        job_id=last.job_id,
                        git_ref=last.git_ref,
                        runtime_started_at=last.runtime_started_at,
                        finished_at=finished,
                        health=(
                            EnumDevRollHealth.HEALTHY
                            if healthy
                            else EnumDevRollHealth.UNHEALTHY
                        ),
                        healthy_since=finished if healthy else None,
                    )
                }
            )
        )
        return ModelDevRollGateRollRecorded(
            command_correlation_id=request.correlation_id,
            outcome="finished_healthy" if healthy else "finished_unhealthy",
            observed_at=now,
        )
