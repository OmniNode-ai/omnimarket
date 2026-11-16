# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerHostDiskLowReported: the low-disk event route of the host disk orchestrator (OMN-23866).

A host reporting it is low starts a run at once, outside the schedule and without moving its last-run time.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.handlers.scheduled_overlay_refusal import ScheduledOverlayRefusalLatch
from omnimarket.models.model_host_disk_decision import ModelHostDiskPolicy
from omnimarket.models.model_host_disk_orchestrator import (
    ModelHostDiskLowReported,
    ModelHostDiskOrchestratorOverlay,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_record_read import (
    HOST_DISK_STATE,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.handler_host_disk_scheduled import (
    NODE_NAME,
    load_policy,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_core import start_run
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_state import (
    HostDiskOrchestratorState,
)

HANDLER_ID = f"{NODE_NAME}.host_disk.low_reported"


class HandlerHostDiskLowReported:
    """ORCHESTRATOR handler: a low-disk event in; commands, alarms and a run record out."""

    def __init__(
        self,
        *,
        overlay: ModelHostDiskOrchestratorOverlay | None = None,
        state: HostDiskOrchestratorState | None = None,
        policy_loader: Callable[[], ModelHostDiskPolicy] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._state = HOST_DISK_STATE if state is None else state
        self._policy_loader = policy_loader or (lambda: load_policy(environ))
        self._environ = environ
        self._refusals = ScheduledOverlayRefusalLatch(NODE_NAME)

    def handle(self, request: ModelHostDiskLowReported) -> ModelHandlerOutput[None]:
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnimarket:host-disk-low:{request.host_name}:{request.reported_at.isoformat()}",
        )
        try:
            overlay = self._overlay or load_structured_overlay(
                NODE_NAME, ModelHostDiskOrchestratorOverlay, self._environ
            )
            policy = self._policy_loader()
        except NodeOverlayNotFoundError as exc:
            self._refusals.refuse(exc)
            events: tuple[ModelEventEnvelope[object], ...] = ()
        else:
            self._refusals.recovered()
            events = start_run(
                self._state,
                overlay,
                policy,
                now=request.reported_at,
                trigger="low_disk",
                run_key=f"low-{request.host_name}-{request.reported_at.strftime('%Y%m%dT%H%M%SZ')}",
            )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id=HANDLER_ID,
            events=events,
        )


__all__: list[str] = ["HandlerHostDiskLowReported"]
