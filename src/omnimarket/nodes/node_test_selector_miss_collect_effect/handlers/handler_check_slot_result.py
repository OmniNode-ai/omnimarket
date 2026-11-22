# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCheckSlotResult: the dead-man check, keyed by the nightly's slot date, run by the lab status tick.

It is its own process and reads neither the gateway nor the collector's switch, so it raises its alarm when the nightly
never started, when the collector is disabled and when the collector crashed before recording a result.
"""

from __future__ import annotations

from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers.alarm_upsert import (
    raise_alarms,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers.handler_collect_nightly import (
    slot_deadline,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ANY_FILE,
    EnumAlarmKind,
    EnumSlotStatus,
    ModelMissCollectPolicy,
    ModelMissCollectSettings,
    ModelNightlyAlarm,
    ModelSlotCheckRequest,
    ModelSlotCheckResult,
    OwnerRoleUnsetError,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    ProtocolAlarmReceiver,
    ProtocolAlarmStore,
)


class HandlerCheckSlotResult:
    def __init__(
        self,
        *,
        settings: ModelMissCollectSettings,
        policy: ModelMissCollectPolicy,
        store: ProtocolAlarmStore,
        receiver: ProtocolAlarmReceiver,
    ) -> None:
        if not settings.owner_role.strip():
            raise OwnerRoleUnsetError("owner_role is blank; refusing to start")
        self._settings = settings
        self._policy = policy
        self._store = store
        self._receiver = receiver

    def handle(self, request: ModelSlotCheckRequest) -> ModelSlotCheckResult:
        settings = self._settings
        deadline = slot_deadline(request.slot_date, self._policy)
        if self._store.has_collector_result(settings.repo, request.slot_date):
            return ModelSlotCheckResult(
                status=EnumSlotStatus.RESULT_RECORDED,
                slot_date=request.slot_date,
                deadline=deadline,
            )
        if request.now < deadline:
            return ModelSlotCheckResult(
                status=EnumSlotStatus.WITHIN_LAG,
                slot_date=request.slot_date,
                deadline=deadline,
                reading_state="UNKNOWN",
            )
        alarm = ModelNightlyAlarm(
            repo=settings.repo,
            slot_date=request.slot_date,
            test_file=ANY_FILE,
            kind=EnumAlarmKind.NO_COLLECTOR_RESULT,
            owner_role=settings.owner_role,
            route=settings.alarm_route,
            text=(
                f"{settings.repo} nightly {request.slot_date.isoformat()} (owner role {settings.owner_role}): "
                f"no collector result within {self._policy.collector_max_lag_hours} hours of the slot; "
                "the night is not a clean night and the selector reading is UNKNOWN for it"
            ),
        )
        created, delivered, failed = raise_alarms(self._store, self._receiver, (alarm,))
        return ModelSlotCheckResult(
            status=EnumSlotStatus.ALARMED,
            slot_date=request.slot_date,
            deadline=deadline,
            alarms_created=created,
            alarms_delivered=delivered,
            delivery_failures=failed,
            reading_state="UNKNOWN",
        )


__all__: list[str] = ["HandlerCheckSlotResult"]
