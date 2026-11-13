# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host disk reading handlers for the host hygiene orchestrator (OMN-23707)."""

from __future__ import annotations

import uuid

from omnibase_core.models.dispatch.model_handler_output import ModelHandlerOutput

from omnimarket.models.model_host_disk_reading import (
    ModelHostDiskRead,
    ModelHostDiskReadFailed,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.contract_config import (
    max_hosts,
)
from omnimarket.nodes.node_host_hygiene_orchestrator.handlers.host_disk_readings import (
    HostDiskReadingStore,
)

HOST_DISK_READINGS = HostDiskReadingStore(max_hosts=max_hosts())


class HandlerHostHygieneRecordRead:
    """ORCHESTRATOR handler: a host disk read in, held; nothing emitted."""

    def __init__(self, store: HostDiskReadingStore | None = None) -> None:
        self._store = HOST_DISK_READINGS if store is None else store

    def handle(self, request: ModelHostDiskRead) -> ModelHandlerOutput[None]:
        self._store.record_read(request)
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnimarket:host-hygiene-read:{request.host_name}:{request.read_at.isoformat()}",
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_host_hygiene_orchestrator.record_host_disk_read",
            events=(),
        )


class HandlerHostHygieneRecordReadFailed:
    """ORCHESTRATOR handler: a host disk read failure in, held; nothing emitted."""

    def __init__(self, store: HostDiskReadingStore | None = None) -> None:
        self._store = HOST_DISK_READINGS if store is None else store

    def handle(self, request: ModelHostDiskReadFailed) -> ModelHandlerOutput[None]:
        self._store.record_failed(request)
        correlation_id = uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"omnimarket:host-hygiene-read-failed:{request.host_name}:{request.read_at.isoformat()}",
        )
        return ModelHandlerOutput.for_orchestrator(
            input_envelope_id=correlation_id,
            correlation_id=correlation_id,
            handler_id="node_host_hygiene_orchestrator.record_host_disk_read_failed",
            events=(),
        )


__all__: list[str] = [
    "HOST_DISK_READINGS",
    "HandlerHostHygieneRecordRead",
    "HandlerHostHygieneRecordReadFailed",
]
