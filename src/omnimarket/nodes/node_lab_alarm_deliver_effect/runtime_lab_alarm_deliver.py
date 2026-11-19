# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the delivery handler on the overlay's backend (the caller is node_lab_alarm's runtime)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

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


class DeliveryAdapters(NamedTuple):
    channel: ProtocolAlarmChannel
    ledger: ProtocolAlarmLedger
    state_at: Callable[[Path], ProtocolDeliveryStateStore]


def _inmemory(overlay: ModelDeliveryOverlay) -> DeliveryAdapters:
    return DeliveryAdapters(
        channel=InMemoryAlarmChannel(),
        ledger=InMemoryAlarmLedger(),
        state_at=lambda _path: InMemoryDeliveryState(),
    )


def _runtime(overlay: ModelDeliveryOverlay) -> DeliveryAdapters:
    return DeliveryAdapters(
        channel=ChannelFromLibrary(
            Path(overlay.channel.library_path), overlay.channel.timeout_seconds
        ),
        ledger=BusLedger(
            overlay.ledger.bus_lane,
            Path(overlay.ledger.workspace_root),
            overlay.ledger.lane,
            overlay.ledger.timeout_seconds,
            socket.gethostname().split(".", 1)[0],
        ),
        state_at=FileDeliveryState,
    )


_BACKEND_BUILDERS: dict[str, Callable[[ModelDeliveryOverlay], DeliveryAdapters]] = {
    "inmemory": _inmemory,
    "runtime": _runtime,
}


def build_delivery_adapters(overlay: ModelDeliveryOverlay) -> DeliveryAdapters:
    """Resolve the channel, ledger and state factory from the delivery overlay."""
    return _BACKEND_BUILDERS[overlay.backend](overlay)


def build_handler(
    overlay: ModelDeliveryOverlay, *, clock: Callable[[], datetime] = _utc_now
) -> HandlerLabAlarmDeliver:
    """The delivery handler wired to the overlay's backend."""
    adapters = build_delivery_adapters(overlay)
    return HandlerLabAlarmDeliver(
        overlay=overlay,
        channel=adapters.channel,
        ledger=adapters.ledger,
        state=adapters.state_at(Path(overlay.state_path)),
        clock=clock,
    )


__all__ = ["DeliveryAdapters", "build_delivery_adapters", "build_handler"]
