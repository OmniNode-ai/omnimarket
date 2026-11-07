# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ProtocolLaneHostPool adapters for the lane start: an in-memory snapshot and a snapshot file (OMN-22848).

The live read of every host (the runner's placement reader) is not part of this task; the caller
that starts the lane (the fill, or the runner's shim) writes the snapshot it read, and the leader
places from it.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelLaneHostPoolRequest,
    ModelLaneHostPoolSnapshot,
)


class AdapterInMemoryHostPool:
    """Answer every read with the snapshot it was given."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, snapshot: ModelLaneHostPoolSnapshot) -> None:
        self._snapshot = snapshot
        self.reads: list[ModelLaneHostPoolRequest] = []

    def read_pool(self, request: ModelLaneHostPoolRequest) -> ModelLaneHostPoolSnapshot:
        self.reads.append(request)
        return self._snapshot


class AdapterSnapshotFileHostPool:
    """Read the snapshot from a JSON file at each read."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(self, path: Path) -> None:
        self._path = path

    def read_pool(self, request: ModelLaneHostPoolRequest) -> ModelLaneHostPoolSnapshot:
        return ModelLaneHostPoolSnapshot.model_validate_json(
            self._path.read_text(encoding="utf-8")
        )


__all__ = ["AdapterInMemoryHostPool", "AdapterSnapshotFileHostPool"]
