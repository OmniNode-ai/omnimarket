# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The host disk read route of the host disk orchestrator (OMN-23866): hold the newest reading per host."""

from __future__ import annotations

import uuid

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.models.model_host_disk_reading import ModelHostDiskRead
from omnimarket.nodes.node_host_disk_orchestrator.handlers.run_state import (
    HostDiskOrchestratorState,
    InMemoryHostDiskRows,
)

HOST_DISK_STATE = HostDiskOrchestratorState()


class HandlerHostDiskRecordRead:
    """ORCHESTRATOR handler: a host disk read in, held; nothing emitted."""

    def __init__(self, state: HostDiskOrchestratorState | None = None) -> None:
        self._state = HOST_DISK_STATE if state is None else state

    def handle(self, request: ModelHostDiskRead) -> ModelHandlerOutput[None]:
        rows = self._state.rows
        if isinstance(rows, InMemoryHostDiskRows):
            rows.record_read(request)
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnimarket:host-disk-read:{request.host_name}:{request.read_at.isoformat()}",
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_host_disk_orchestrator.record_host_disk_read",
            events=(),
        )


__all__: list[str] = ["HOST_DISK_STATE", "HandlerHostDiskRecordRead"]
