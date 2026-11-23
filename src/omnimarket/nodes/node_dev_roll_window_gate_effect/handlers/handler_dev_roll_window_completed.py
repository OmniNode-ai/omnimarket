# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completed handler of node_dev_roll_window_gate_effect (OMN-19998).

The deploy agent's completion of the NEWEST command the gate released ends the roll the
gate started: it records the finish and the health, which the batching decision reads to
hold the next release until the previous roll has finished and has been healthy for the
overlay's bound. The agent folds a batch into one roll of its newest command, and runs an
unfoldable batch in order, so the newest completes last either way. A completion for any
other command changes nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from omnimarket.events.runtime_deployment import (
    EnumRedeployStatus,
    ModelDeployRebuildCompleted,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.dev_roll import EnumDevRollHealth, ModelDevRollRecord
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers.gate_context import (
    DevRollGateContext,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateRollOutcome,
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
        try:
            store = self._context.store()
        except NodeOverlayNotFoundError:
            return None  # no gate overlay installed: this lane's completions are not the gate's
        state = store.load()
        last = state.last_roll
        if last is None or request.correlation_id != last.job_id:
            return None
        if last.finished_at is not None:
            return None
        now = self._context.now()
        completed_at = request.completed_at
        finished = (
            completed_at
            if completed_at is not None and completed_at.utcoffset() is not None
            else now
        )
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
            outcome=(
                EnumDevRollGateRollOutcome.FINISHED_HEALTHY
                if healthy
                else EnumDevRollGateRollOutcome.FINISHED_UNHEALTHY
            ),
            observed_at=now,
        )
