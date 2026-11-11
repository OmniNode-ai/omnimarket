# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the lane host pool snapshot step (OMN-23664): its four declared sources, its store and its publisher."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnibase_spi.contracts.services import ModelLaneHostPoolSnapshot
from pydantic import BaseModel

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostRow
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelHostAccounts,
    ModelPoolSnapshotRecord,
)


class ProtocolHostAdvertisementSource(Protocol):
    """The host capacity advertisements; a source that cannot read raises PoolSnapshotSourceError, never returns empty."""

    name: str

    def read_advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]: ...


class ProtocolHostTableSource(Protocol):
    """The lab host table rows; a source that cannot read raises PoolSnapshotSourceError, never returns empty."""

    name: str

    def read_hosts(self) -> tuple[ModelLabHostRow, ...]: ...


class ProtocolRunningLanesSource(Protocol):
    """The open lane count per host name at the given time."""

    name: str

    def running_lanes(self, now: datetime) -> dict[str, int]: ...


class ProtocolHostAccountsSource(Protocol):
    """The per-host token pool account labels; a source that cannot read raises PoolSnapshotSourceError, never returns empty."""

    name: str

    def read_accounts(
        self, hosts: tuple[ModelLabHostRow, ...]
    ) -> tuple[ModelHostAccounts, ...]: ...


class ProtocolPoolSnapshotStore(Protocol):
    """Writes the snapshot file and the record beside it; reads the record back."""

    name: str

    def write(
        self, snapshot: ModelLaneHostPoolSnapshot, record: ModelPoolSnapshotRecord
    ) -> None: ...

    def read_record(self) -> ModelPoolSnapshotRecord | None: ...


class ProtocolPoolSnapshotPublisher(Protocol):
    def publish(self, topic: str, event: BaseModel) -> None: ...


__all__: list[str] = [
    "ProtocolHostAccountsSource",
    "ProtocolHostAdvertisementSource",
    "ProtocolHostTableSource",
    "ProtocolPoolSnapshotPublisher",
    "ProtocolPoolSnapshotStore",
    "ProtocolRunningLanesSource",
]
