# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port wiring of the lane host pool snapshot step (OMN-23664): the sources, store and publisher the overlay's backend selects.

The redpanda profile's paths, routes and bounds all come from the overlay; the in-memory backend returns empty in-memory ports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAccountsSource,
    InMemoryHostAdvertisementSource,
    InMemoryHostTableSource,
    InMemoryPoolSnapshotPublisher,
    InMemoryPoolSnapshotStore,
    InMemoryRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_sources import (
    AdvertisementCommandSource,
    AdvertisementFileSource,
    LabHostTableSource,
    LedgerRunningLanesSource,
    TokenPoolCommandAccountsSource,
    resolve_path,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot_overlay import (
    EnumPoolSnapshotBackend,
    ModelPoolSnapshotOverlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.protocols.protocol_pool_snapshot import (
    ProtocolHostAccountsSource,
    ProtocolHostAdvertisementSource,
    ProtocolHostTableSource,
    ProtocolPoolSnapshotPublisher,
    ProtocolPoolSnapshotStore,
    ProtocolRunningLanesSource,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.adapter_publisher import (
    KafkaSnapshotPublisher,
    LaneBusSnapshotPublisher,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    LedgerRouteError,
    build_row_source,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ProtocolLedgerRowSource,
)


def _row_source(
    route: ModelFileLedgerReadRoute | ModelCommandLedgerReadRoute,
    environ: Mapping[str, str],
) -> ProtocolLedgerRowSource:
    """The reading writer's ledger row source for the overlay's route; an unset variable it names is a refused overlay."""
    try:
        return build_row_source(route, environ)
    except LedgerRouteError as exc:
        raise ValueError(f"ledger_read route refused: {exc}") from None


@dataclass(frozen=True)
class PoolSnapshotPorts:
    """The six ports one pass reads and writes through."""

    advertisements: ProtocolHostAdvertisementSource
    host_table: ProtocolHostTableSource
    lanes: ProtocolRunningLanesSource
    accounts: ProtocolHostAccountsSource
    store: ProtocolPoolSnapshotStore
    publisher: ProtocolPoolSnapshotPublisher


def build_pool_snapshot_ports(
    overlay: ModelPoolSnapshotOverlay,
    *,
    environ: Mapping[str, str],
    runner: Callable[[Sequence[str], int], tuple[int, str]] | None = None,
    publisher: ProtocolPoolSnapshotPublisher | None = None,
) -> PoolSnapshotPorts:
    """The ports of the overlay's backend; the redpanda profile reads every path, route and bound from the overlay."""
    if overlay.backend is EnumPoolSnapshotBackend.INMEMORY:
        return PoolSnapshotPorts(
            advertisements=InMemoryHostAdvertisementSource(()),
            host_table=InMemoryHostTableSource(()),
            lanes=InMemoryRunningLanesSource({}),
            accounts=InMemoryHostAccountsSource(()),
            store=InMemoryPoolSnapshotStore(),
            publisher=publisher or InMemoryPoolSnapshotPublisher(),
        )
    route = overlay.advertisements
    if (
        route is None
        or overlay.accounts is None
        or not overlay.snapshot_path
        or overlay.ledger_read is None
    ):
        raise ValueError("the redpanda backend needs its overlay fields")
    advertisements: ProtocolHostAdvertisementSource
    if route.path:
        advertisements = AdvertisementFileSource(resolve_path(route.path))
    elif route.argv:
        advertisements = AdvertisementCommandSource(
            route.argv, route.timeout_seconds, runner
        )
    else:
        raise ValueError("the advertisement route names no path or argv")
    if publisher is None:
        if overlay.bus_lane and overlay.workspace_root:
            publisher = LaneBusSnapshotPublisher(
                overlay.bus_lane, resolve_path(overlay.workspace_root)
            )
        elif overlay.bootstrap_servers:
            publisher = KafkaSnapshotPublisher(overlay.bootstrap_servers)
        else:
            raise ValueError(
                "the redpanda backend names one of bus_lane or bootstrap_servers"
            )
    return PoolSnapshotPorts(
        advertisements=advertisements,
        host_table=LabHostTableSource(environ),
        lanes=LedgerRunningLanesSource(
            _row_source(overlay.ledger_read, environ),
            overlay.ledger_lookback_hours,
        ),
        accounts=TokenPoolCommandAccountsSource(overlay.accounts, runner),
        store=FileSnapshotStore(
            resolve_path(overlay.snapshot_path), overlay.record_suffix
        ),
        publisher=publisher,
    )


__all__: list[str] = ["PoolSnapshotPorts", "build_pool_snapshot_ports"]
