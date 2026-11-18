# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the delivery handler on the overlay's backend (the caller is node_lab_alarm's runtime)."""

from __future__ import annotations

import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

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


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _inmemory(
    overlay: ModelDeliveryOverlay, clock: Callable[[], datetime]
) -> HandlerLabAlarmDeliver:
    return HandlerLabAlarmDeliver(
        overlay=overlay,
        channel=InMemoryAlarmChannel(),
        ledger=InMemoryAlarmLedger(),
        state=InMemoryDeliveryState(),
        clock=clock,
    )


def _runtime(
    overlay: ModelDeliveryOverlay, clock: Callable[[], datetime]
) -> HandlerLabAlarmDeliver:
    return HandlerLabAlarmDeliver(
        overlay=overlay,
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
        state=FileDeliveryState(Path(overlay.state_path)),
        clock=clock,
    )


_BACKEND_BUILDERS: dict[
    str,
    Callable[[ModelDeliveryOverlay, Callable[[], datetime]], HandlerLabAlarmDeliver],
] = {"inmemory": _inmemory, "runtime": _runtime}


def build_handler(
    overlay: ModelDeliveryOverlay, *, clock: Callable[[], datetime] = _utc_now
) -> HandlerLabAlarmDeliver:
    """The delivery handler wired to the overlay's backend."""
    return _BACKEND_BUILDERS[overlay.backend](overlay, clock)


__all__ = ["build_handler"]
