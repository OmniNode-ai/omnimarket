# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_bus_login_exposure_scheduled_effect: the bus login exposure probe on the runtime tick (OMN-24271)."""

from omnimarket.nodes.node_bus_login_exposure_scheduled_effect.handlers.handler_bus_login_exposure_scheduled import (
    HandlerBusLoginExposureScheduled,
)

__all__ = ["HandlerBusLoginExposureScheduled", "NodeBusLoginExposureScheduledEffect"]


class NodeBusLoginExposureScheduledEffect(HandlerBusLoginExposureScheduled):
    """ONEX entry-point wrapper for HandlerBusLoginExposureScheduled."""
