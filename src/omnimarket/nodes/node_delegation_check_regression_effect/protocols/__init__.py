# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ports of the delegation check regression run."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.models.lab_alarm_delivery import (
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    ModelCheckReading,
)


class ProtocolCheckRunner(Protocol):
    def run(
        self, check_id: str, *, since: datetime, timeout_seconds: int
    ) -> ModelCheckReading | None:
        """Run the check after ``since`` and return its reading, or None when none arrived inside the timeout."""
        ...


class ProtocolCheckHistory(Protocol):
    def previous(self, check_id: str, *, before: datetime) -> ModelCheckReading | None:
        """The check's newest reading recorded before the change, or None when it has none."""
        ...


class ProtocolAlarmDelivery(Protocol):
    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        """The lab alarm delivery (node_lab_alarm_deliver_effect), or raise ValueError or OSError."""
        ...


__all__ = ["ProtocolAlarmDelivery", "ProtocolCheckHistory", "ProtocolCheckRunner"]
