# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builders shared by the lane host pool snapshot tests (OMN-23664): advertisements, host table rows, accounts, an overlay and a handler."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.overlay_keys import ModelLabHostRow
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.handler_pool_snapshot import (
    HandlerPoolSnapshot,
    default_overlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAccountsSource,
    InMemoryHostAdvertisementSource,
    InMemoryHostTableSource,
    InMemoryPoolSnapshotPublisher,
    InMemoryPoolSnapshotStore,
    InMemoryRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models import (
    ModelHostAccounts,
    ModelPoolSnapshotOverlay,
    ModelPoolSnapshotRequest,
)

T0 = datetime(2026, 11, 11, 3, 0, tzinfo=UTC)
CORRELATION = UUID("00000000-0000-4000-8000-000000000001")


def advertisement(
    host: str,
    *,
    cores: int = 8,
    load1: float = 1.0,
    mem_gb: int = 32,
    advertised_at: datetime = T0,
    capacity_refusal: str = "",
    max_units: int = 4,
) -> ModelHostCapacityAdvertisement:
    return ModelHostCapacityAdvertisement(
        host_name=host,
        cores=cores,
        load1=load1,
        mem_available_bytes=mem_gb * 2**30,
        advertised_at=advertised_at,
        cadence_seconds=15,
        max_units=max_units,
        capacity_refusal=capacity_refusal,
    )


def host_row(
    name: str,
    *,
    available: bool = True,
    unavailable_reason: str | None = None,
    cap: int | None = None,
    local: bool = False,
    contributor: bool = False,
    lane_ready: bool = False,
    codex: bool = False,
) -> ModelLabHostRow:
    return ModelLabHostRow(
        name=name,
        target=name,
        available=available,
        unavailable_reason=unavailable_reason,
        cap=cap,
        local=local,
        contributor=contributor,
        lane_ready=lane_ready,
        codex=codex,
    )


def accounts(
    host: str,
    usable: tuple[str, ...] = ("acct-a",),
    limited: tuple[str, ...] = (),
    unread_reason: str | None = None,
) -> ModelHostAccounts:
    return ModelHostAccounts(
        host=host, usable=usable, limited=limited, unread_reason=unread_reason
    )


def overlay(**changes: Any) -> ModelPoolSnapshotOverlay:
    """The in-memory profile of the shipped example overlay (synthetic values), with ``changes`` applied."""
    return default_overlay().model_copy(update=changes)


def clock(at: datetime = T0) -> Callable[[], datetime]:
    return lambda: at


def handler(
    *,
    ads: tuple[ModelHostCapacityAdvertisement, ...] = (),
    rows: tuple[ModelLabHostRow, ...] = (),
    lanes: dict[str, int] | None = None,
    accs: tuple[ModelHostAccounts, ...] = (),
    store: InMemoryPoolSnapshotStore | None = None,
    publisher: InMemoryPoolSnapshotPublisher | None = None,
    at: datetime = T0,
    overlay_: ModelPoolSnapshotOverlay | None = None,
) -> HandlerPoolSnapshot:
    return HandlerPoolSnapshot(
        overlay=overlay() if overlay_ is None else overlay_,
        advertisement_source=InMemoryHostAdvertisementSource(ads),
        host_table_source=InMemoryHostTableSource(rows),
        lanes_source=InMemoryRunningLanesSource(lanes or {}),
        accounts_source=InMemoryHostAccountsSource(accs),
        store=InMemoryPoolSnapshotStore() if store is None else store,
        publisher=InMemoryPoolSnapshotPublisher() if publisher is None else publisher,
        clock=clock(at),
    )


def pool_request() -> ModelPoolSnapshotRequest:
    return ModelPoolSnapshotRequest(correlation_id=CORRELATION, write=True)
