# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-backed store and pool adapters for the lane host pool snapshot (OMN-23664).

``FileSnapshotStore`` writes the snapshot and the record beside it atomically.
``StampedSnapshotFileLaneHostPool`` refuses a stale or missing record, then delegates
the actual read to the shared ``SnapshotFileLaneHostPool`` from the lane start node.
``SnapshotFloorPool`` projects the further lanes each host can take for the floor fill.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import ClassVar

from omnibase_spi.contracts.services import (
    ModelLaneHostPoolRequest,
    ModelLaneHostPoolSnapshot,
)
from omnibase_spi.protocols.lane import ProtocolLaneHostPool

from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotSourceError,
    PoolSnapshotStaleError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelPoolSnapshotRecord,
)
from omnimarket.nodes.node_remote_lane_start_orchestrator.handlers.adapter_host_pool import (
    SnapshotFileLaneHostPool,
)


class FileSnapshotStore:
    """Write the snapshot and record to disk atomically; read the record back."""

    name = "snapshot-file-store"

    def __init__(self, path: Path, record_suffix: str) -> None:
        self._path = path
        self._record_path = Path(str(path) + record_suffix)

    def write(
        self,
        snapshot: ModelLaneHostPoolSnapshot,
        record: ModelPoolSnapshotRecord,
    ) -> None:
        self._atomic_write(self._path, snapshot.model_dump_json())
        self._atomic_write(self._record_path, record.model_dump_json())

    def read_record(self) -> ModelPoolSnapshotRecord | None:
        if not self._record_path.exists():
            return None
        try:
            return ModelPoolSnapshotRecord.model_validate_json(
                self._record_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise PoolSnapshotSourceError(
                f"record file {self._record_path} is unparseable: {exc}"
            ) from exc

    def _atomic_write(self, target: Path, payload: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            os.replace(tmp, str(target))
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise PoolSnapshotSourceError(
                f"atomic write of {target} failed: {exc}"
            ) from exc


class StampedSnapshotFileLaneHostPool:
    """Refuse a stale or missing record, then read the snapshot file."""

    synchronous_execution: ClassVar[bool] = True

    def __init__(
        self,
        path: Path,
        record_suffix: str,
        max_age_seconds: int,
        clock: Callable[[], datetime],
    ) -> None:
        self._path = path
        self._record_suffix = record_suffix
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        self._store = FileSnapshotStore(path, record_suffix)

    def read_pool(self, request: ModelLaneHostPoolRequest) -> ModelLaneHostPoolSnapshot:
        try:
            record = self._store.read_record()
        except PoolSnapshotSourceError as exc:
            raise PoolSnapshotStaleError(str(exc)) from exc
        if record is None:
            raise PoolSnapshotStaleError(
                f"snapshot record missing at {self._path}{self._record_suffix}"
            )
        now = self._clock()
        age = (now - record.read_at).total_seconds()
        if age > self._max_age_seconds:
            raise PoolSnapshotStaleError(
                f"snapshot record stale: read_at={record.read_at.isoformat()} "
                f"age={age:.0f}s > {self._max_age_seconds}s"
            )
        return SnapshotFileLaneHostPool(self._path).read_pool(request)


class SnapshotFloorPool:
    """Sum the further lanes each host can take for the floor fill."""

    def __init__(self, pool: ProtocolLaneHostPool, engine: str) -> None:
        self._pool = pool
        self._engine = engine

    def free_slots(self) -> int:
        try:
            snapshot = self._pool.read_pool(
                ModelLaneHostPoolRequest(engine=self._engine)
            )
        except (PoolSnapshotSourceError, OSError, ValueError) as exc:
            raise FloorFillUnreadableError(f"pool-snapshot:{exc}") from exc
        total = 0
        for reading in snapshot.readings:
            if reading.lane_admission_refusal is None:
                total += reading.lane_slots
        return total


__all__: list[str] = [
    "FileSnapshotStore",
    "SnapshotFloorPool",
    "StampedSnapshotFileLaneHostPool",
]
