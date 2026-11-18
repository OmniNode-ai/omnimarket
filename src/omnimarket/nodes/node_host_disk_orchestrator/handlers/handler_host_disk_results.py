# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The result routes of the host disk orchestrator (OMN-23866): collect each typed result or typed failure.

A result is matched to the open run that sent its idempotency key. The run record is published when its last
awaited result arrives. A result for no open run (a redelivery, or one after the deadline closed the run) is held
nowhere and changes nothing.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.models.model_host_disk_orchestrator import (
    ModelHostDiskCommandOutcome,
    ModelHostDiskEvictCompleted,
    ModelHostDiskEvictFailed,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_record_read import (
    HOST_DISK_STATE,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_core import close_run
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_state import (
    HostDiskOrchestratorState,
)

NODE_NAME = "node_host_disk_orchestrator"


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _collect(
    state: HostDiskOrchestratorState,
    outcome: ModelHostDiskCommandOutcome,
    handler_id: str,
    now: dt.datetime,
) -> ModelHandlerOutput[None]:
    correlation_id = uuid.uuid5(
        uuid.NAMESPACE_URL, f"omnimarket:host-disk-result:{outcome.idempotency_key}"
    )
    finished = state.runs.record(outcome)
    events: tuple[ModelEventEnvelope[object], ...] = ()
    if finished is not None:
        # the run closes at its last result, so its finish time is that result's arrival
        events = (close_run(finished, finished_at=now),)
    return ModelHandlerOutput.for_orchestrator(
        input_envelope_id=correlation_id,
        correlation_id=correlation_id,
        handler_id=handler_id,
        events=events,
    )


class HandlerHostDiskEvictCompleted:
    """ORCHESTRATOR handler: a typed result in, collected."""

    def __init__(
        self,
        state: HostDiskOrchestratorState | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._state = HOST_DISK_STATE if state is None else state
        self._clock = clock

    def handle(self, request: ModelHostDiskEvictCompleted) -> ModelHandlerOutput[None]:
        return _collect(
            self._state,
            ModelHostDiskCommandOutcome(
                idempotency_key=request.idempotency_key,
                host_name=request.host_name,
                class_name=request.class_name,
                status="completed",
                evicted_count=request.evicted_count,
                bytes_freed=request.bytes_freed,
                detail=request.detail,
            ),
            f"{NODE_NAME}.host_disk.evict_completed",
            self._clock(),
        )


class HandlerHostDiskEvictFailed:
    """ORCHESTRATOR handler: a typed failure in, collected."""

    def __init__(
        self,
        state: HostDiskOrchestratorState | None = None,
        clock: Callable[[], dt.datetime] = _utc_now,
    ) -> None:
        self._state = HOST_DISK_STATE if state is None else state
        self._clock = clock

    def handle(self, request: ModelHostDiskEvictFailed) -> ModelHandlerOutput[None]:
        return _collect(
            self._state,
            ModelHostDiskCommandOutcome(
                idempotency_key=request.idempotency_key,
                host_name=request.host_name,
                class_name=request.class_name,
                status="failed",
                failure_reason=request.reason,
                detail=request.detail,
            ),
            f"{NODE_NAME}.host_disk.evict_failed",
            self._clock(),
        )


__all__: list[str] = ["HandlerHostDiskEvictCompleted", "HandlerHostDiskEvictFailed"]
