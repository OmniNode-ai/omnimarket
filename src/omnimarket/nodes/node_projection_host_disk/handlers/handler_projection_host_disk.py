# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pure fold over one host disk read event (OMN-23865).

Rule 7a definition-B: ``handle(event) -> result``. No clock, no broker, no
database. Every time the rows carry is a field of the event, so the same event
always folds to the same rows and a replay is byte-identical.

This class is half of the pair. The writer beside it is the entry the runtime
calls; a pure entry alone on a projection arm validates, returns and stores
nothing while every watermark reads healthy (rule 7a, OMN-18769).
"""

from __future__ import annotations

from omnimarket.events.host_disk_reading import (
    ModelHostDiskRead,
)
from omnimarket.nodes.node_projection_host_disk.models import (
    ModelHostDiskClassRow,
    ModelHostDiskProjectionResult,
    ModelHostDiskVolumeRow,
)


class HandlerProjectionHostDisk:
    """Folds one host disk read event into one row per class and per volume."""

    def handle(self, request: ModelHostDiskRead) -> ModelHostDiskProjectionResult:
        class_rows = tuple(
            ModelHostDiskClassRow(
                host_name=request.host_name,
                class_name=reading.class_name,
                status=reading.status,
                unread_reason=reading.unread_reason,
                total_bytes=reading.total_bytes,
                entry_count=reading.entry_count,
                revision_count=reading.revision_count,
                # JSON-ready dicts, not the frozen event models: the row is the
                # value the projection's groups column stores, byte for byte.
                groups=tuple(group.model_dump(mode="json") for group in reading.groups),
                groups_omitted=reading.groups_omitted,
                unreadable_process_count=reading.unreadable_process_count,
                read_at=request.read_at,
                correlation_id=request.correlation_id,
            )
            for reading in request.classes
        )
        volume_rows = tuple(
            ModelHostDiskVolumeRow(
                host_name=request.host_name,
                volume_path=volume.path,
                total_bytes=volume.total_bytes,
                free_bytes=volume.free_bytes,
                used_bytes=volume.used_bytes,
                read_at=request.read_at,
                correlation_id=request.correlation_id,
            )
            for volume in request.volumes
        )
        return ModelHostDiskProjectionResult(
            host_name=request.host_name,
            correlation_id=request.correlation_id,
            read_at=request.read_at,
            class_rows=class_rows,
            volume_rows=volume_rows,
        )


__all__ = ["HandlerProjectionHostDisk"]
