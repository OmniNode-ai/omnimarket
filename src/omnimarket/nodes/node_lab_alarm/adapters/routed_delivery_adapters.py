# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the severity router and the daily digest on the delivery overlay's backend (OMN-24939).

The delivery node's adapters are constructed with the delivery overlay's own values, unchanged, as
node_lab_alarm_deliver_effect's builder constructs them; the one difference is that the delivery handler is handed
the router's gate in front of the channel, so the router decides per request whether a post goes out. The channel,
its credentials and the ledger write path stay the delivery node's adapters.
"""

from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from omnimarket.nodes.node_lab_alarm.handlers.alarm_route import (
    GatedAlarmChannel,
    HandlerAlarmSeverityRoute,
    load_alarm_severity_table,
)
from omnimarket.nodes.node_lab_alarm.handlers.handler_alarm_digest import (
    HandlerAlarmDigest,
)
from omnimarket.nodes.node_lab_alarm.models.model_alarm_severity import (
    ModelAlarmSeverityTable,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.runtime_adapters import (
    BusLedger,
    ChannelFromLibrary,
    FileDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    ProtocolAlarmChannel,
    ProtocolAlarmLedger,
    ProtocolDeliveryStateStore,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class _Parts(NamedTuple):
    channel: ProtocolAlarmChannel
    ledger: ProtocolAlarmLedger
    state: ProtocolDeliveryStateStore
    digest: ProtocolDeliveryStateStore


def _inmemory_parts(
    _overlay: ModelDeliveryOverlay, _table: ModelAlarmSeverityTable
) -> _Parts:
    return _Parts(
        InMemoryAlarmChannel(),
        InMemoryAlarmLedger(),
        InMemoryDeliveryState(),
        InMemoryDeliveryState(),
    )


def _runtime_parts(
    overlay: ModelDeliveryOverlay, table: ModelAlarmSeverityTable
) -> _Parts:
    state_path = Path(overlay.state_path)
    return _Parts(
        ChannelFromLibrary(
            Path(overlay.channel.library_path), overlay.channel.timeout_seconds
        ),
        BusLedger(
            overlay.ledger.bus_lane,
            Path(overlay.ledger.workspace_root),
            overlay.ledger.lane,
            overlay.ledger.timeout_seconds,
            socket.gethostname().split(".", 1)[0],
        ),
        FileDeliveryState(state_path),
        FileDeliveryState(state_path.parent / table.routing.digest_state_file),
    )


_PARTS_BUILDERS: dict[
    str, Callable[[ModelDeliveryOverlay, ModelAlarmSeverityTable], _Parts]
] = {"inmemory": _inmemory_parts, "runtime": _runtime_parts}


def _parts(overlay: ModelDeliveryOverlay, table: ModelAlarmSeverityTable) -> _Parts:
    return _PARTS_BUILDERS[overlay.backend](overlay, table)


def require_declared(
    alarm_overlay: ModelLabAlarmOverlay, table: ModelAlarmSeverityTable
) -> None:
    """The broker lag's condition name is the overlay's; it must carry a declared severity too."""
    table.source(alarm_overlay.lag.record_condition)


def build_routed_delivery(
    delivery_overlay: ModelDeliveryOverlay,
    *,
    alarm_overlay: ModelLabAlarmOverlay | None = None,
    table: ModelAlarmSeverityTable | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> HandlerAlarmSeverityRoute:
    """The delivery wrapped by the severity router; an undeclared source refuses the build."""
    table = table or load_alarm_severity_table()
    if alarm_overlay is not None:
        require_declared(alarm_overlay, table)
    parts = _parts(delivery_overlay, table)
    gate = GatedAlarmChannel(parts.channel)
    inner = HandlerLabAlarmDeliver(
        overlay=delivery_overlay,
        channel=gate,
        ledger=parts.ledger,
        state=parts.state,
        clock=clock,
    )
    return HandlerAlarmSeverityRoute(
        table=table,
        gate=gate,
        channel=parts.channel,
        inner=inner,
        digest=parts.digest,
        clock=clock,
    )


def build_alarm_digest(
    delivery_overlay: ModelDeliveryOverlay,
    *,
    table: ModelAlarmSeverityTable | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> HandlerAlarmDigest:
    """The daily digest on the same channel, ledger and digest store the router holds into."""
    table = table or load_alarm_severity_table()
    parts = _parts(delivery_overlay, table)
    return HandlerAlarmDigest(
        table=table,
        channel=parts.channel,
        ledger=parts.ledger,
        digest=parts.digest,
        lane=delivery_overlay.ledger.lane,
        actor=delivery_overlay.ledger.actor,
        request_namespace=delivery_overlay.ledger.request_namespace,
        clock=clock,
    )


__all__ = ["build_alarm_digest", "build_routed_delivery", "require_declared"]
