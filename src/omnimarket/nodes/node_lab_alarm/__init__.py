# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_lab_alarm: the lab alarm's conditions as a node, starting with broker lag (OMN-23024)."""

from omnimarket.nodes.node_lab_alarm.handlers.handler_broker_lag import (
    HandlerBrokerLag,
)

__all__ = ["HandlerBrokerLag"]
