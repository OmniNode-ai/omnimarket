# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Injected ports of the host repair dispatch (OMN-23389): durable state, measurement, dispatch, escalation."""

from __future__ import annotations

from typing import Protocol

from omnimarket.nodes.node_lab_host_repair_dispatch_effect.models.model_host_repair import (
    ModelHostMeasurement,
    ModelRepairLaneSpec,
    ModelRepairState,
)


class ProtocolRepairStateStore(Protocol):
    def get(self, key: str) -> ModelRepairState | None: ...

    def put(self, state: ModelRepairState) -> None: ...

    def delete(self, key: str) -> None: ...


class ProtocolHostMeasurement(Protocol):
    def measure(self, host: str) -> ModelHostMeasurement:
        """The df and findmnt reading taken on ``host``; raises ``MeasurementError`` when it cannot be read."""
        ...


class ProtocolRepairDispatcher(Protocol):
    def dispatch(self, spec: ModelRepairLaneSpec) -> None:
        """Start the lane; raises ``DispatchError`` when it was not started."""
        ...


class ProtocolEscalationSink(Protocol):
    def escalate(self, row: str) -> None: ...
