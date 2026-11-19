# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Builds the severity router and the daily digest on the delivery overlay's backend (OMN-24939).

The delivery node's adapters are constructed with the delivery overlay's own values, unchanged, as
node_lab_alarm_deliver_effect's builder constructs them; the one difference is that the delivery handler is handed
the router's gate in front of the channel, so the router decides per request whether a post goes out. The channel,
its credentials and the ledger write path stay the delivery node's adapters.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.nodes.node_lab_alarm.handlers.handler_alarm_digest import (
    GatedAlarmChannel,
    HandlerAlarmDigest,
    HandlerAlarmRoute,
    load_alarm_severity_table,
)
from omnimarket.nodes.node_lab_alarm.models.model_alarm_severity import (
    ModelAlarmSeverityTable,
)
from omnimarket.nodes.node_lab_alarm.overlay import ModelLabAlarmOverlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_delivery_adapters,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
) -> HandlerAlarmRoute:
    """The delivery wrapped by the severity router; an undeclared source refuses the build."""
    table = table or load_alarm_severity_table()
    if alarm_overlay is not None:
        require_declared(alarm_overlay, table)
    adapters = build_delivery_adapters(delivery_overlay)
    state_path = Path(delivery_overlay.state_path)
    gate = GatedAlarmChannel(adapters.channel)
    inner = HandlerLabAlarmDeliver(
        overlay=delivery_overlay,
        channel=gate,
        ledger=adapters.ledger,
        state=adapters.state_at(state_path),
        clock=clock,
    )
    return HandlerAlarmRoute(
        table=table,
        gate=gate,
        channel=adapters.channel,
        inner=inner,
        digest=adapters.state_at(state_path.parent / table.routing.digest_state_file),
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
    adapters = build_delivery_adapters(delivery_overlay)
    state_path = Path(delivery_overlay.state_path)
    return HandlerAlarmDigest(
        table=table,
        channel=adapters.channel,
        ledger=adapters.ledger,
        digest=adapters.state_at(state_path.parent / table.routing.digest_state_file),
        lane=delivery_overlay.ledger.lane,
        actor=delivery_overlay.ledger.actor,
        request_namespace=delivery_overlay.ledger.request_namespace,
        clock=clock,
    )


__all__ = ["build_alarm_digest", "build_routed_delivery", "require_declared"]
