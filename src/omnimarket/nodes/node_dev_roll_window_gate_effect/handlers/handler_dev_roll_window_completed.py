# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Completed handler of node_dev_roll_window_gate_effect (OMN-19998).

The deploy agent's completion of a command the gate released ends the roll the gate started: it records the
finish and the health, which the batching decision reads to hold the next release until the previous roll has
finished and has been healthy for the overlay's bound.

Any command of the released batch ends the roll, not only the newest. A deploy-agent instance publishes a
completion only for a command it ran; a command the dev routing table gives to another instance is skipped by
the instance it was not routed to, with nothing published. A batch whose newest command is a skipped one would
otherwise never end, and the gate would hold every later merge until ``roll_completion_deadline_seconds``. The
roll keeps its newest command as its id. The agent folds a batch into one roll of its newest and runs an
unfoldable batch in order, so a later completion of the same batch than the one recorded replaces the
recorded finish and health (the newest completes last, as it always did); a completion that is not later, a
repeat of one, and a completion of a command outside the batch change nothing.
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
        if last is None:
            return None
        batch = {*state.last_roll_command_ids, last.job_id}
        if request.correlation_id not in batch:
            return None
        now = self._context.now()
        completed_at = request.completed_at
        stamped = completed_at is not None and completed_at.utcoffset() is not None
        if last.finished_at is not None and (
            not stamped or completed_at is None or completed_at <= last.finished_at
        ):
            return None  # the roll already ended, and this is not a later completion of its batch
        finished = completed_at if stamped and completed_at is not None else now
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
