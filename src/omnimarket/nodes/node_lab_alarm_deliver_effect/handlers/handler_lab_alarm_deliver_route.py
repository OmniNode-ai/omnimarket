# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delivery as a runtime-attached handler: a command on the contract's topic reaches the delivery (OMN-25270).

HandlerLabAlarmDeliver takes its overlay, channel, ledger, episode state and clock as constructor parameters, so a
runtime that builds a handler from its class cannot construct it. This handler has no required constructor
parameter: on its first request it loads the node's overlay (``ONEX_SKILL_OVERLAY_ROOTS``) and builds the delivery on
the backend that overlay names, then hands every request to it unchanged. A request that does not write (``write``
false, a probe or a drill) returns the delivery's SKIPPED result and delivers nothing.
"""

from __future__ import annotations

from collections.abc import Callable

from omnimarket.models.lab_alarm_delivery import (
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import load_overlay
from omnimarket.nodes.node_lab_alarm_deliver_effect.runtime_lab_alarm_deliver import (
    build_handler,
)


def _build_from_overlay() -> HandlerLabAlarmDeliver:
    return build_handler(load_overlay())


class HandlerLabAlarmDeliverRoute:
    """Builds the delivery from the node's overlay on first use; delivers what the request carries."""

    def __init__(
        self, *, build: Callable[[], HandlerLabAlarmDeliver] = _build_from_overlay
    ) -> None:
        self._build = build
        self._delivery: HandlerLabAlarmDeliver | None = None

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        if self._delivery is None:
            self._delivery = self._build()
        return self._delivery.handle(request)


__all__ = ["HandlerLabAlarmDeliverRoute"]
