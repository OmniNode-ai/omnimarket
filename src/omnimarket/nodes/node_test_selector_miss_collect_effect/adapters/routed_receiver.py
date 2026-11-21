# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The alarm receiver over the alarm routing's delivery.

The delivery is edge-triggered per ``condition:subject`` (a subject already in an episode is suppressed), so the subject
is the alarm's key and the receiver is idempotent by it.
"""

from __future__ import annotations

from typing import Literal

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ModelNightlyAlarm,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    ProtocolAlarmRoutingDelivery,
)


class RoutedAlarmReceiver:
    def __init__(self, delivery: ProtocolAlarmRoutingDelivery) -> None:
        self._delivery = delivery

    def accept(self, alarm: ModelNightlyAlarm) -> Literal["accepted", "duplicate"]:
        request = ModelLabAlarmDeliveryRequest(
            correlation_id=alarm.key,
            condition=alarm.route,
            outcome=EnumDeliveryCondition.ALARM,
            alarms=(
                ModelLabAlarm(
                    condition=alarm.route, subject=alarm.key, detail=alarm.text
                ),
            ),
            evidence=f"owner role {alarm.owner_role}",
        )
        result = self._delivery.handle(request)
        if result.outcome is EnumDeliveryOutcome.FAILED:
            raise OSError(f"alarm delivery failed: {result.failure}")
        if result.outcome is EnumDeliveryOutcome.DELIVERED:
            return "accepted"
        return "duplicate"


__all__: list[str] = ["RoutedAlarmReceiver"]
