# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of node_broker_load_reading_effect: the broker's metrics and the probe consumer."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_broker_load_reading_effect.models.model_broker_load import (
    ModelBrokerMetrics,
    ModelConsumerStart,
)


class BrokerMetricsError(RuntimeError):
    """The broker's metrics could not be read or did not hold the counts."""


class ProtocolBrokerMetrics(Protocol):
    def read(self) -> ModelBrokerMetrics:
        """The broker's counts; raises ``BrokerMetricsError`` with a typed reason."""
        ...


class ProtocolConsumerStart(Protocol):
    def measure(self, deadline_seconds: float) -> ModelConsumerStart:
        """Start one probe consumer and report how long it took; never raises."""
        ...


__all__ = [
    "BrokerMetricsError",
    "ProtocolBrokerMetrics",
    "ProtocolConsumerStart",
]
