# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the delivery handler on the overlay's backend (the caller is node_lab_alarm's runtime)."""

from __future__ import annotations

import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.alarm_post_policy import AlarmPostGate, run_ticket_command
from omnimarket.models.alarm_post_policy import ModelPostDecision
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.policy_gated_channel import (
    PolicyGatedChannel,
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
)


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


def _gated(
    channel: ProtocolAlarmChannel,
    overlay: ModelDeliveryOverlay,
    clock: Callable[[], datetime],
) -> ProtocolAlarmChannel:
    """The channel behind the overlay's post policy; ungated when the overlay declares none."""
    policy = overlay.post_policy
    if policy is None:
        return channel
    if not overlay.post_policy_state_path:
        raise ValueError("post_policy is declared with no post_policy_state_path")
    return PolicyGatedChannel(
        inner=channel,
        gate=AlarmPostGate(policy),
        state=FileDeliveryState(Path(overlay.post_policy_state_path)),
        clock=clock,
        reading=_log_reading,
        ticket=lambda decision: run_ticket_command(policy.ticket_command, decision),
    )


def _log_reading(decision: ModelPostDecision, text: str) -> None:
    """The reading of a message that did not post, kept on stderr for the runtime's log shipper."""
    sys.stderr.write(
        f"alarm-reading kind={decision.kind} key={decision.key} reason={decision.reason}\n"
    )


def _runtime(
    overlay: ModelDeliveryOverlay, clock: Callable[[], datetime]
) -> HandlerLabAlarmDeliver:
    return HandlerLabAlarmDeliver(
        overlay=overlay,
        channel=_gated(
            ChannelFromLibrary(
                Path(overlay.channel.library_path), overlay.channel.timeout_seconds
            ),
            overlay,
            clock,
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
