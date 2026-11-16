# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-memory defaults of the lane host pool snapshot step's ports (OMN-23664)."""

from __future__ import annotations

from datetime import datetime

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


class InMemoryHostAdvertisementSource:
    """The host capacity advertisements, as given."""

    name = "inmemory-advertisements"

    def __init__(self, ads: tuple[ModelHostCapacityAdvertisement, ...]) -> None:
        self._ads = tuple(ads)

    def read_advertisements(self) -> tuple[ModelHostCapacityAdvertisement, ...]:
        return self._ads


class InMemoryHostTableSource:
    """The lab host table rows, as given."""

    name = "inmemory-host-table"

    def __init__(self, rows: tuple[ModelLabHostRow, ...]) -> None:
        self._rows = tuple(rows)

    def read_hosts(self) -> tuple[ModelLabHostRow, ...]:
        return self._rows


class InMemoryRunningLanesSource:
    """The open lane count per host name, as given."""

    name = "inmemory-running-lanes"

    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = dict(counts)

    def running_lanes(self, now: datetime) -> dict[str, int]:
        return dict(self._counts)


class InMemoryHostAccountsSource:
    """The per-host token pool account labels, as given."""

    name = "inmemory-accounts"

    def __init__(self, accounts: tuple[ModelHostAccounts, ...]) -> None:
        self._accounts = tuple(accounts)

    def read_accounts(
        self, hosts: tuple[ModelLabHostRow, ...]
    ) -> tuple[ModelHostAccounts, ...]:
        return self._accounts


class InMemoryPoolSnapshotStore:
    """Keeps the last snapshot and record it was given, and the list of writes."""

    name = "inmemory-store"

    def __init__(self) -> None:
        self.snapshot: ModelLaneHostPoolSnapshot | None = None
        self.record: ModelPoolSnapshotRecord | None = None
        self.writes: list[
            tuple[ModelLaneHostPoolSnapshot, ModelPoolSnapshotRecord]
        ] = []

    def write(
        self, snapshot: ModelLaneHostPoolSnapshot, record: ModelPoolSnapshotRecord
    ) -> None:
        self.snapshot = snapshot
        self.record = record
        self.writes.append((snapshot, record))

    def read_record(self) -> ModelPoolSnapshotRecord | None:
        return self.record


class InMemoryPoolSnapshotPublisher:
    """Records the events it is given, with their topic."""

    name = "inmemory-publisher"

    def __init__(self) -> None:
        self.published: list[tuple[str, BaseModel]] = []

    def publish(self, topic: str, event: BaseModel) -> None:
        self.published.append((topic, event))


__all__: list[str] = [
    "InMemoryHostAccountsSource",
    "InMemoryHostAdvertisementSource",
    "InMemoryHostTableSource",
    "InMemoryPoolSnapshotPublisher",
    "InMemoryPoolSnapshotStore",
    "InMemoryRunningLanesSource",
]
