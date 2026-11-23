# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerPublishAttributionRecord: publish one public-safe attribution record per night through the repository gateway.

The record is bound to the nightly run it describes: the run id and the tested head commit are read through the gateway
by the night's slot date, never taken from the caller, and the record is written as a check run on that commit. One record
per ``(repository, nightly slot date)``: the record is upserted under its key before the write, the gateway write is
idempotent by ``(repository, head commit, check run name)``, and delivery is retried from the store until the row is
``published``, so a rerun and two overlapping invocations publish one record, and a crash between the write and its
completion leaves one record after the retry (the retry finds the check run and only marks the row). A night with no
collector result publishes nothing; the slot check raises the alarm after ``collector_max_lag_hours``.
"""

from __future__ import annotations

from omnimarket.models.test_selector_reading import EnumAttributionClass
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers.public_safety_guard import (
    assert_public_safe,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_attribution_record import (
    EnumPublishStatus,
    EnumRecordStatus,
    ModelPublishedAttributionRecord,
    ModelPublishRecordRequest,
    ModelPublishRecordResult,
    RecordBindingError,
    check_run_for,
    check_run_name,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    EnumCollectStatus,
    ModelMissCollectPolicy,
    ModelMissCollectSettings,
    ModelNightlyRun,
    OwnerRoleUnsetError,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.ports import (
    GatewayReadError,
    ProtocolAlarmStore,
    ProtocolNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.record_ports import (
    GatewayWriteError,
    ProtocolRecordGateway,
    ProtocolRecordStore,
)


class HandlerPublishAttributionRecord:
    """EFFECT handler. One record per night by its key, no lease."""

    def __init__(
        self,
        *,
        settings: ModelMissCollectSettings,
        policy: ModelMissCollectPolicy,
        night: ProtocolNightlyGateway,
        alarms: ProtocolAlarmStore,
        records: ProtocolRecordStore,
        gateway: ProtocolRecordGateway,
    ) -> None:
        if not settings.owner_role.strip():
            raise OwnerRoleUnsetError("owner_role is blank; refusing to start")
        self._settings = settings
        self._policy = policy
        self._night = night
        self._alarms = alarms
        self._records = records
        self._gateway = gateway

    def handle(self, request: ModelPublishRecordRequest) -> ModelPublishRecordResult:
        settings, slot_date = self._settings, request.slot_date
        name = check_run_name(self._policy.record_check_name_prefix, slot_date)

        def outcome(
            status: EnumPublishStatus,
            record: ModelPublishedAttributionRecord | None = None,
            failure: str = "",
        ) -> ModelPublishRecordResult:
            return ModelPublishRecordResult(
                status=status,
                slot_date=slot_date,
                record=record,
                check_run_name=name,
                failure=failure,
            )

        if not settings.collector_enabled:
            return outcome(EnumPublishStatus.DISABLED)
        collected = request.collect_result
        if collected.slot_date != slot_date:
            raise RecordBindingError(
                "the collect result describes another nightly slot date than the request"
            )
        if not self._alarms.has_collector_result(settings.repo, slot_date):
            return outcome(EnumPublishStatus.NO_COLLECTOR_RESULT)
        if (
            collected.status is not EnumCollectStatus.COLLECTED
            or collected.attribution is None
        ):
            return outcome(EnumPublishStatus.NOT_COLLECTED)
        attribution = collected.attribution
        if attribution.repo != settings.repo or attribution.slot_date != slot_date:
            raise RecordBindingError(
                "the attribution describes another repository or night than the request"
            )
        try:
            run = self._night.nightly_run(settings.repo, slot_date)
            if run is None:
                return outcome(EnumPublishStatus.NO_RUN)
            pr_records = self._pr_record_count(run, request)
        except GatewayReadError as exc:
            return outcome(EnumPublishStatus.READ_FAILED, failure=str(exc))
        counts = dict.fromkeys(EnumAttributionClass, 0)
        for item in attribution.attributions:
            counts[item.klass] += 1
        incomplete = not attribution.complete
        try:
            record = ModelPublishedAttributionRecord(
                repository=settings.repo,
                nightly_slot_date=slot_date,
                nightly_run_id=run.run_id,
                tested_head_sha=run.head_sha,
                selector_version=request.selector_version,
                policy_version=request.policy_version,
                stage_entry_counted=request.stage_entry_counted,
                pull_request_record_count=pr_records,
                miss_count=counts[EnumAttributionClass.MISS],
                interaction_count=counts[EnumAttributionClass.INTERACTION],
                preexisting_count=counts[EnumAttributionClass.PREEXISTING],
                unattributable_count=counts[EnumAttributionClass.UNATTRIBUTABLE],
                incomplete_count=len(attribution.incomplete_reasons)
                if incomplete
                else 0,
                status=EnumRecordStatus.INCOMPLETE
                if incomplete
                else EnumRecordStatus.COMPLETE,
            )
        except ValueError as exc:
            if _is_head_sha_error(exc):
                raise RecordBindingError(
                    "the nightly run's tested head is not a full commit id"
                ) from exc
            raise
        assert_public_safe(record, settings, self._policy)
        created = self._records.upsert_pending(record)
        delivered, failed = self._deliver_pending()
        stored = self._records.get(settings.repo, slot_date) or record
        if stored.key in failed:
            return outcome(
                EnumPublishStatus.WRITE_PENDING,
                record=stored,
                failure=failed[stored.key],
            )
        if stored.key in delivered or created:
            return outcome(EnumPublishStatus.PUBLISHED, record=stored)
        return outcome(EnumPublishStatus.ALREADY_PUBLISHED, record=stored)

    def _pr_record_count(
        self, run: ModelNightlyRun, request: ModelPublishRecordRequest
    ) -> int:
        """Merged pull requests in the night's range whose record holds an entry for the stage being entered."""
        settings = self._settings
        previous = self._night.previous_night(settings.repo, request.slot_date)
        if previous is None:
            return 0
        candidates = self._night.candidates(
            settings.repo,
            previous.head_sha,
            run.head_sha,
            self._policy.record_retention_days,
        )
        return sum(
            1
            for c in candidates
            if c.pr_number is not None
            and c.record is not None
            and request.stage_entry_counted in c.record.by_stage
        )

    def _deliver_pending(self) -> tuple[set[str], dict[str, str]]:
        """Write every unpublished record, then mark it. Returns (delivered keys, failures by key)."""
        delivered: set[str] = set()
        failed: dict[str, str] = {}
        for pending in self._records.unpublished():
            check_run = check_run_for(pending, self._policy.record_check_name_prefix)
            try:
                self._gateway.ensure_check_run(pending.repository, check_run)
            except GatewayWriteError as exc:
                failed[pending.key] = str(exc)
                continue
            self._records.mark_published(pending.key)
            delivered.add(pending.key)
        return delivered, failed


def _is_head_sha_error(exc: ValueError) -> bool:
    errors = getattr(exc, "errors", None)
    if errors is None:
        return False
    return any(e["loc"] == ("tested_head_sha",) for e in errors())


__all__: list[str] = ["HandlerPublishAttributionRecord"]
