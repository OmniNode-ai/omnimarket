# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane principal readback and reply capability checks for lab callers.

Long-running hosts use grouped subscriptions. One-shot callers require the
ungrouped reply port before publishing any command.
"""

from __future__ import annotations

from omnimarket.protocols.protocol_lab_run_bus import ProtocolLabRunBus
from omnimarket.protocols.protocol_one_shot_reply_bus import ProtocolOneShotReplyBus


class LabRunBusError(RuntimeError):
    """The bus could not be selected or opened."""


class HandlerOneShotReplyBus:
    """Admit a bus with the group-less reply capability before a one-shot call."""

    def handle(self, bus: ProtocolLabRunBus) -> ProtocolOneShotReplyBus:
        """The bus a one-shot request/reply caller reads its reply from (OMN-17427).

        A caller that reads one reply and leaves must not join a consumer group: the
        group outlives it on the broker. The bus either answers the group-less read
        or the caller is refused here, at construction, never at the broker.
        """
        if not isinstance(bus, ProtocolOneShotReplyBus):
            raise LabRunBusError(
                f"{type(bus).__name__} has no group-less reply read (subscribe_ungrouped); "
                "a one-shot caller would leave a consumer group on the broker"
            )
        return bus


require_one_shot_reply_bus = HandlerOneShotReplyBus().handle


def bound_lane_principal() -> str:
    """Return the broker login the open lane bus connection authenticated as.

    Valid only inside ``open_lab_run_bus(bus="kafka", ...)``, which binds the
    lane transport for the life of the connection.
    """
    from omnibase_infra.event_bus.lane_client_transport_binding import (
        bound_lane_client_transport,
    )

    transport = bound_lane_client_transport()
    if transport is None or not transport.sasl_username:
        raise LabRunBusError(
            "this lane resolves no broker login: the writer identity is the broker principal"
        )
    return transport.sasl_username
