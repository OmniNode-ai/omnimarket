# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCollectNightly: read one nightly, check it is complete, attribute its red files and raise the alarms."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from omnimarket.models.test_selector_reading import (
    ATTRIBUTION_LIMITS,
    EnumAttributionClass,
    ModelAttributionCandidate,
    ModelFileAttribution,
    ModelNightCompleteness,
    ModelNightlyAttribution,
    ModelNightlyAttributionRequest,
    attribute_nightly_failures,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers.alarm_upsert import (
    raise_alarms,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ANY_FILE,
    EnumAlarmKind,
    EnumCollectStatus,
    ModelCollectRequest,
    ModelCollectResult,
    ModelMissCollectPolicy,
    ModelMissCollectSettings,
    ModelNightlyAlarm,
    OwnerRoleUnsetError,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    GatewayReadError,
    ProtocolAlarmReceiver,
    ProtocolAlarmStore,
    ProtocolNightlyGateway,
)

_ALARMING = {
    EnumAttributionClass.MISS: EnumAlarmKind.MISS,
    EnumAttributionClass.UNATTRIBUTABLE: EnumAlarmKind.UNATTRIBUTABLE,
}


def slot_deadline(slot_date: date, policy: ModelMissCollectPolicy) -> datetime:
    """When the collector's result for the slot is overdue: the slot time plus ``collector_max_lag_hours``."""
    slot = datetime.combine(slot_date, policy.nightly_slot_time_utc, tzinfo=UTC)
    return slot + timedelta(hours=policy.collector_max_lag_hours)


def _text(settings: ModelMissCollectSettings, slot_date: date, body: str) -> str:
    limits = "; ".join(ATTRIBUTION_LIMITS)
    return f"{settings.repo} nightly {slot_date.isoformat()} (owner role {settings.owner_role}): {body}. Limits: {limits}."


def _file_alarm(
    settings: ModelMissCollectSettings, slot_date: date, item: ModelFileAttribution
) -> ModelNightlyAlarm:
    detail = f"{item.test_file} is red on the dev head: {item.klass.value}"
    if item.suspects:
        detail += f", suspects {', '.join(item.suspects)}"
    if item.reason is not None:
        detail += f", reason {item.reason.value}"
    if item.shadow:
        detail += " (shadow: would skip)"
    return ModelNightlyAlarm(
        repo=settings.repo,
        slot_date=slot_date,
        test_file=item.test_file,
        kind=_ALARMING[item.klass],
        owner_role=settings.owner_role,
        route=settings.alarm_route,
        text=_text(settings, slot_date, detail),
    )


class HandlerCollectNightly:
    """EFFECT handler. One writer per night by its alarm key, no lease."""

    def __init__(
        self,
        *,
        settings: ModelMissCollectSettings,
        policy: ModelMissCollectPolicy,
        gateway: ProtocolNightlyGateway,
        store: ProtocolAlarmStore,
        receiver: ProtocolAlarmReceiver,
        shadow_stage: int | None = None,
    ) -> None:
        if not settings.owner_role.strip():
            raise OwnerRoleUnsetError("owner_role is blank; refusing to start")
        self._settings = settings
        self._policy = policy
        self._gateway = gateway
        self._store = store
        self._receiver = receiver
        self._shadow_stage = shadow_stage

    def handle(self, request: ModelCollectRequest) -> ModelCollectResult:
        settings, policy = self._settings, self._policy
        slot_date = request.slot_date
        if not settings.collector_enabled:
            return ModelCollectResult(
                status=EnumCollectStatus.DISABLED, slot_date=slot_date
            )
        try:
            run = self._gateway.nightly_run(settings.repo, slot_date)
            if run is None:
                return ModelCollectResult(
                    status=EnumCollectStatus.NO_RUN, slot_date=slot_date
                )
            if not run.completed and request.now < slot_deadline(slot_date, policy):
                return ModelCollectResult(
                    status=EnumCollectStatus.WAITING, slot_date=slot_date
                )
            reading = self._gateway.read_night(settings.repo, run)
            previous = self._gateway.previous_night(settings.repo, slot_date)
            candidates: tuple[ModelAttributionCandidate, ...] = ()
            if previous is not None:
                candidates = self._gateway.candidates(
                    settings.repo,
                    previous.head_sha,
                    run.head_sha,
                    policy.record_retention_days,
                )
        except GatewayReadError as exc:
            # Nothing is recorded: the night is neither clean nor attributed, and the slot check raises
            # the dead-man alarm when no later invocation reads it.
            return ModelCollectResult(
                status=EnumCollectStatus.READ_FAILED,
                slot_date=slot_date,
                failure=str(exc),
            )
        attribution = attribute_nightly_failures(
            ModelNightlyAttributionRequest(
                repo=settings.repo,
                slot_date=slot_date,
                completeness=ModelNightCompleteness(
                    run_terminal=run.completed,
                    shards_expected=policy.nightly_shard_count,
                    artifacts_found=reading.artifacts_found,
                    shards_in_error=reading.shards_in_error,
                    test_files_reported=len(reading.outcomes),
                    min_test_files=policy.nightly_min_test_files,
                ),
                outcomes=reading.outcomes,
                previous_outcomes=previous.outcomes if previous else {},
                candidates=candidates,
                shadow_stage=self._shadow_stage,
            )
        )
        alarms = self._alarms(slot_date, attribution)
        created, delivered, failed = raise_alarms(self._store, self._receiver, alarms)
        self._store.record_collector_result(settings.repo, slot_date)
        return ModelCollectResult(
            status=EnumCollectStatus.COLLECTED,
            slot_date=slot_date,
            attribution=attribution,
            alarms_created=created,
            alarms_delivered=delivered,
            delivery_failures=failed,
        )

    def _alarms(
        self, slot_date: date, attribution: ModelNightlyAttribution
    ) -> tuple[ModelNightlyAlarm, ...]:
        settings = self._settings
        if not attribution.complete:
            reasons = ", ".join(r.value for r in attribution.incomplete_reasons)
            return (
                ModelNightlyAlarm(
                    repo=settings.repo,
                    slot_date=slot_date,
                    test_file=ANY_FILE,
                    kind=EnumAlarmKind.INCOMPLETE,
                    owner_role=settings.owner_role,
                    route=settings.alarm_route,
                    text=_text(
                        settings,
                        slot_date,
                        f"the night is incomplete ({reasons}) and is not a clean night",
                    ),
                ),
            )
        return tuple(
            _file_alarm(settings, slot_date, item)
            for item in attribution.attributions
            if item.klass in _ALARMING
        )


__all__: list[str] = ["HandlerCollectNightly", "slot_deadline"]
