# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports of the install-silence alert effect: the projection read, the lane rows and the alarm."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from omnimarket.models.lab_alarm_delivery import (
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallObservedEvent,
)


class ProtocolHookInstallEventsReader(Protocol):
    def read(self, since: datetime) -> Sequence[ModelHookInstallObservedEvent]:
        """Events of claude_hook_events at or after `since`, per host, actor and time. Raises when unreadable."""
        ...


class ProtocolHookInstallAlarm(Protocol):
    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        """The existing lab alarm delivery effect."""
        ...


__all__ = ["ProtocolHookInstallAlarm", "ProtocolHookInstallEventsReader"]
