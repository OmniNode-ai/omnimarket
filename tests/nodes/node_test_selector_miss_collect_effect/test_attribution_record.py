# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly's public-safe attribution record: its model, the keyed publication through the gateway, the public-safety
guard and the incomplete or missing night. Focused names: record_model, record_dedup, record_public_safe,
record_incomplete_night."""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from omnimarket.models.test_selector_reading import (
    EnumFileOutcome,
    EnumShardConclusion,
    ModelAttributionCandidate,
    ModelCandidateRecord,
    ModelCandidateRun,
    ModelCandidateSelection,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_adapters import (
    InMemoryAlarmReceiver,
    InMemoryAlarmStore,
    InMemoryNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_record_adapters import (
    InMemoryRecordGateway,
    InMemoryRecordStore,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers import (
    HandlerCheckSlotResult,
    HandlerCollectNightly,
    HandlerPublishAttributionRecord,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_attribution_record import (
    EnumPublishStatus,
    EnumRecordStatus,
    ModelPublishedAttributionRecord,
    ModelPublishRecordRequest,
    ModelRecordCheckRun,
    PublicSafetyError,
    RecordBindingError,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    EnumAlarmKind,
    ModelCollectRequest,
    ModelMissCollectSettings,
    ModelNightlyRun,
    ModelNightReading,
    ModelPreviousNight,
    ModelSlotCheckRequest,
    load_policy,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.record_ports import (
    GatewayWriteError,
)

pytestmark = pytest.mark.unit

REPO = "example-org/example-repo"
SLOT = date(2026, 11, 21)
HEAD = "a" * 40
POLICY = load_policy()
RED = "tests/nodes/node_x/test_x.py"
MERGED = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)
OVERLAY: dict[str, Any] = {
    "repo": REPO,
    "workflow_file": "nightly-full-suite.yml",
    "owner_role": "example-test-selection-owner",
    "alarm_route": "test_selector_miss",
    "collector_enabled": True,
    "status_tick": "example-lab-status-tick",
    "public_repositories": [REPO],
    "private_terms": ["example-secret-term"],
}
SKIPS_RED = ModelCandidateSelection(
    selected_paths=("tests/nodes/node_y/",), split_count=1
)


class Crash(BaseException):
    """An injected process crash: not an Exception, so no handler swallows it."""


def _settings(**over: Any) -> ModelMissCollectSettings:
    return ModelMissCollectSettings.load({**OVERLAY, **over})


def _files(red: bool) -> dict[str, EnumFileOutcome]:
    files = {
        f"tests/bulk/test_{n}.py": EnumFileOutcome.PASSED
        for n in range(POLICY.nightly_min_test_files)
    }
    files[RED] = EnumFileOutcome.FAILED if red else EnumFileOutcome.PASSED
    return files


def _candidate(sha: str, stages: tuple[int, ...] = (0,)) -> ModelAttributionCandidate:
    selection = ModelCandidateSelection(selected_paths=("tests/",), split_count=1)
    return ModelAttributionCandidate(
        commit_sha=sha,
        pr_number=7,
        merged_at=MERGED,
        record=ModelCandidateRecord(
            checkout_sha=f"co-{sha}",
            executed=SKIPS_RED,
            by_stage=dict.fromkeys(stages, selection),
        ),
        run=ModelCandidateRun(
            run_id=5,
            attempt=1,
            checkout_sha=f"co-{sha}",
            completed_at=MERGED - timedelta(minutes=3),
            shard_conclusions={1: EnumShardConclusion.SUCCESS},
        ),
    )


def _night(
    *,
    red: bool = False,
    artifacts: int | None = None,
    candidates: tuple[ModelAttributionCandidate, ...] = (),
    head: str = HEAD,
    fail_reads: bool = False,
) -> InMemoryNightlyGateway:
    return InMemoryNightlyGateway(
        run=ModelNightlyRun(run_id=100, completed=True, head_sha=head),
        reading=ModelNightReading(
            artifacts_found=POLICY.nightly_shard_count
            if artifacts is None
            else artifacts,
            shards_in_error=0,
            outcomes=_files(red),
        ),
        previous=ModelPreviousNight(head_sha="b" * 40, outcomes=_files(False)),
        candidates=candidates,
        fail_reads=fail_reads,
    )


class Rig:
    """One lab host's wiring: the alarm store the collector writes, and the record store and gateway."""

    def __init__(
        self, night: InMemoryNightlyGateway | None = None, **over: Any
    ) -> None:
        self.night = night or _night()
        self.alarms = InMemoryAlarmStore()
        self.receiver = InMemoryAlarmReceiver()
        self.records = InMemoryRecordStore()
        self.gateway = InMemoryRecordGateway()
        self.over = over

    def collector(self) -> HandlerCollectNightly:
        return HandlerCollectNightly(
            settings=_settings(**self.over),
            policy=POLICY,
            gateway=self.night,
            store=self.alarms,
            receiver=self.receiver,
        )

    def publisher(self) -> HandlerPublishAttributionRecord:
        return HandlerPublishAttributionRecord(
            settings=_settings(**self.over),
            policy=POLICY,
            night=self.night,
            alarms=self.alarms,
            records=self.records,
            gateway=self.gateway,
        )

    def collect(self) -> Any:
        return self.collector().handle(ModelCollectRequest(slot_date=SLOT, now=NOW))

    def request(self, collected: Any, **over: Any) -> ModelPublishRecordRequest:
        fields: dict[str, Any] = {
            "slot_date": SLOT,
            "now": NOW,
            "collect_result": collected,
            "selector_version": "1.0.0",
            "policy_version": "7",
            "stage_entry_counted": 0,
        }
        return ModelPublishRecordRequest(**{**fields, **over})

    def publish(self, **over: Any) -> Any:
        return self.publisher().handle(self.request(self.collect(), **over))


def _record(**over: Any) -> ModelPublishedAttributionRecord:
    fields: dict[str, Any] = {
        "repository": REPO,
        "nightly_slot_date": SLOT,
        "nightly_run_id": 100,
        "tested_head_sha": HEAD,
        "selector_version": "1.0.0",
        "policy_version": "7",
        "stage_entry_counted": 0,
        "pull_request_record_count": 2,
        "miss_count": 0,
        "interaction_count": 0,
        "preexisting_count": 0,
        "unattributable_count": 0,
        "incomplete_count": 0,
        "status": "complete",
    }
    return ModelPublishedAttributionRecord(**{**fields, **over})


# --- AC1: the record model (record_model) -----------------------------------------------------


def test_record_model_has_exactly_the_listed_fields() -> None:
    assert list(ModelPublishedAttributionRecord.model_fields) == [
        "repository",
        "nightly_slot_date",
        "nightly_run_id",
        "tested_head_sha",
        "selector_version",
        "policy_version",
        "stage_entry_counted",
        "pull_request_record_count",
        "miss_count",
        "interaction_count",
        "preexisting_count",
        "unattributable_count",
        "incomplete_count",
        "status",
    ]


def test_record_model_rejects_any_field_outside_the_list() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _record(owner="someone")


def test_record_model_is_frozen() -> None:
    field = "miss_count"
    record = _record()
    with pytest.raises(ValidationError, match="frozen"):
        setattr(record, field, 3)


@pytest.mark.parametrize(
    "over",
    [
        {"tested_head_sha": "head-today"},
        {"nightly_run_id": 0},
        {"miss_count": -1},
        {"stage_entry_counted": 5},
        {"status": "clean"},
        {"selector_version": "a b"},
    ],
)
def test_record_model_refuses_malformed_values(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _record(**over)


def test_record_model_status_and_incomplete_count_agree() -> None:
    with pytest.raises(ValidationError, match="incomplete_count"):
        _record(status="incomplete", incomplete_count=0)
    with pytest.raises(ValidationError, match="incomplete_count"):
        _record(status="complete", incomplete_count=1)
    with pytest.raises(ValidationError, match="attributes no file"):
        _record(status="incomplete", incomplete_count=1, miss_count=1)


def test_record_model_clean_needs_a_complete_night_without_miss_or_unattributable() -> (
    None
):
    assert _record().clean
    assert not _record(miss_count=1).clean
    assert not _record(unattributable_count=1).clean
    assert not _record(status="incomplete", incomplete_count=2).clean


def test_record_model_the_key_is_repository_and_slot_date() -> None:
    assert _record().key == f"{REPO}|2026-11-21"
    assert _record(nightly_run_id=999).key == _record().key


# --- AC2: one record per night (record_dedup) ---------------------------------------------------


def test_record_dedup_the_record_is_bound_to_the_run_and_the_tested_head() -> None:
    rig = Rig(_night(red=True, candidates=(_candidate("c" * 40),)))
    result = rig.publish()
    assert result.status is EnumPublishStatus.PUBLISHED
    ((repo, check_run),) = rig.gateway.check_runs
    assert repo == REPO
    assert check_run.head_sha == HEAD
    assert check_run.name == f"{POLICY.record_check_name_prefix}:2026-11-21"
    body = json.loads(check_run.summary)
    assert body["nightly_run_id"] == 100
    assert body["tested_head_sha"] == HEAD
    assert ModelPublishedAttributionRecord.model_validate(body) == result.record


def test_record_dedup_the_counts_come_from_the_attribution() -> None:
    rig = Rig(_night(red=True, candidates=(_candidate("c" * 40), _candidate("d" * 40))))
    record = rig.publish().record
    assert record is not None
    assert record.miss_count == 1
    assert record.interaction_count == 0
    assert record.status is EnumRecordStatus.COMPLETE
    assert record.pull_request_record_count == 2
    assert not record.clean


def test_record_dedup_the_record_count_counts_the_entry_of_the_stage_entered() -> None:
    night = _night(
        candidates=(_candidate("c" * 40, (0,)), _candidate("d" * 40, (0, 1)))
    )
    rig = Rig(night)
    stage0 = rig.publish(stage_entry_counted=0).record
    other = Rig(night)
    stage1 = other.publish(stage_entry_counted=1).record
    assert stage0 is not None
    assert stage1 is not None
    assert (stage0.pull_request_record_count, stage1.pull_request_record_count) == (
        2,
        1,
    )
    assert stage1.stage_entry_counted == 1


def test_record_dedup_a_rerun_over_the_same_night_publishes_one() -> None:
    rig = Rig()
    first = rig.publish()
    second = rig.publish()
    assert first.status is EnumPublishStatus.PUBLISHED
    assert second.status is EnumPublishStatus.ALREADY_PUBLISHED
    assert len(rig.gateway.check_runs) == 1
    assert len(rig.records.rows) == 1
    assert rig.gateway.writes == 1


def test_record_dedup_two_concurrent_invocations_publish_one() -> None:
    barrier = threading.Barrier(2)
    rig = Rig()
    collected = rig.collect()

    class Gate(InMemoryRecordGateway):
        def ensure_check_run(self, repo: str, check_run: ModelRecordCheckRun) -> Any:
            with contextlib.suppress(threading.BrokenBarrierError):
                barrier.wait(timeout=2)
            return super().ensure_check_run(repo, check_run)

    rig.gateway = Gate()
    results: list[Any] = []
    request = rig.request(collected)
    threads = [
        threading.Thread(target=lambda: results.append(rig.publisher().handle(request)))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(results) == 2
    assert len(rig.gateway.check_runs) == 1
    assert len(rig.records.rows) == 1
    assert rig.gateway.writes == 1


def test_record_dedup_a_crash_after_the_write_and_before_completion_leaves_one_record() -> (
    None
):
    rig = Rig()
    collected = rig.collect()
    crashed = {"done": False}
    real = rig.records.mark_published

    def crash_once(key: str) -> None:
        if not crashed["done"]:
            crashed["done"] = True
            raise Crash
        real(key)

    method = "mark_published"
    setattr(rig.records, method, crash_once)
    with pytest.raises(Crash):
        rig.publisher().handle(rig.request(collected))
    assert len(rig.gateway.check_runs) == 1
    assert not rig.records.is_published(REPO, SLOT)
    retry = rig.publisher().handle(rig.request(collected))
    assert retry.status is EnumPublishStatus.PUBLISHED
    assert len(rig.gateway.check_runs) == 1
    assert rig.gateway.writes == 1
    assert rig.records.is_published(REPO, SLOT)


def test_record_dedup_a_crash_after_the_upsert_and_before_the_write_leaves_one_record() -> (
    None
):
    rig = Rig()
    collected = rig.collect()

    class CrashBeforeWrite(InMemoryRecordGateway):
        def ensure_check_run(self, repo: str, check_run: ModelRecordCheckRun) -> Any:
            raise Crash

    rig.gateway = CrashBeforeWrite()
    with pytest.raises(Crash):
        rig.publisher().handle(rig.request(collected))
    assert len(rig.records.rows) == 1
    rig.gateway = InMemoryRecordGateway()
    retry = rig.publisher().handle(rig.request(collected))
    assert retry.status is EnumPublishStatus.PUBLISHED
    assert len(rig.gateway.check_runs) == 1


def test_record_dedup_a_write_failure_stays_pending_and_the_next_run_delivers() -> None:
    rig = Rig()
    rig.gateway = InMemoryRecordGateway(fail_writes=1)
    first = rig.publish()
    assert first.status is EnumPublishStatus.WRITE_PENDING
    assert first.failure
    assert rig.gateway.check_runs == []
    second = rig.publish()
    assert second.status is EnumPublishStatus.PUBLISHED
    assert len(rig.gateway.check_runs) == 1


def test_record_dedup_the_first_record_of_a_night_stands() -> None:
    rig = Rig()
    first = rig.publish()
    again = rig.publisher().handle(rig.request(rig.collect(), policy_version="8"))
    assert again.status is EnumPublishStatus.ALREADY_PUBLISHED
    assert first.record is not None
    assert first.record.policy_version == "7"
    assert len(rig.gateway.check_runs) == 1


def test_record_dedup_another_night_is_another_record() -> None:
    rig = Rig()
    rig.publish()
    collector = rig.collector()
    next_slot = SLOT + timedelta(days=1)
    collected = collector.handle(
        ModelCollectRequest(slot_date=next_slot, now=NOW + timedelta(days=1))
    )
    rig.publisher().handle(
        rig.request(collected, slot_date=next_slot, now=NOW + timedelta(days=1))
    )
    assert len(rig.gateway.check_runs) == 2
    assert {c.name for _, c in rig.gateway.check_runs} == {
        f"{POLICY.record_check_name_prefix}:2026-11-21",
        f"{POLICY.record_check_name_prefix}:2026-11-22",
    }


# --- AC3: public safety (record_public_safe) ----------------------------------------------------


def _refused(rig: Rig, **over: Any) -> PublicSafetyError:
    with pytest.raises(PublicSafetyError) as raised:
        rig.publish(**over)
    assert rig.gateway.check_runs == []
    assert rig.records.rows == {}
    return raised.value


def test_record_public_safe_a_repository_outside_the_public_list_is_refused() -> None:
    rig = Rig(_night(), public_repositories=["other-org/other-repo"])
    error = _refused(rig)
    assert error.code == "public_safety_refused"
    assert error.field == "repository"
    assert REPO not in str(error)


def test_record_public_safe_an_empty_public_list_refuses_everything() -> None:
    assert _refused(Rig(_night(), public_repositories=[])).field == "repository"


@pytest.mark.parametrize(
    "version",
    [
        "build.corp." + "internal",
        "build.host." + "lan",
        ".".join(["10", "20", "30", "40"]),
        "x-example-secret-term-1",
    ],
)
def test_record_public_safe_a_version_naming_a_host_or_a_private_term_is_refused(
    version: str,
) -> None:
    error = _refused(Rig(), selector_version=version)
    assert error.field == "selector_version"
    assert version not in str(error)


def test_record_public_safe_a_person_term_in_the_policy_version_is_refused() -> None:
    error = _refused(Rig(), policy_version="Example-Secret-Term")
    assert error.field == "policy_version"


def test_record_public_safe_the_denied_patterns_are_policy_keys() -> None:
    assert POLICY.record_denied_patterns
    assert POLICY.record_check_name_prefix


def test_record_public_safe_a_clean_payload_is_published() -> None:
    assert Rig().publish().status is EnumPublishStatus.PUBLISHED


# --- AC4: an incomplete or missing night (record_incomplete_night) ------------------------------


def test_record_incomplete_night_publishes_a_record_that_says_incomplete() -> None:
    rig = Rig(_night(artifacts=POLICY.nightly_shard_count - 1))
    result = rig.publish()
    assert result.status is EnumPublishStatus.PUBLISHED
    record = result.record
    assert record is not None
    assert record.status is EnumRecordStatus.INCOMPLETE
    assert record.incomplete_count >= 1
    assert not record.clean
    ((_, check_run),) = rig.gateway.check_runs
    assert check_run.conclusion != "success"
    assert json.loads(check_run.summary)["status"] == "incomplete"


def test_record_incomplete_night_a_complete_night_is_the_only_clean_one() -> None:
    clean = Rig().publish().record
    missed = Rig(_night(red=True, candidates=(_candidate("c" * 40),))).publish().record
    incomplete = Rig(_night(artifacts=POLICY.nightly_shard_count - 1)).publish().record
    assert clean is not None
    assert missed is not None
    assert incomplete is not None
    assert [clean.clean, missed.clean, incomplete.clean] == [True, False, False]


def test_record_incomplete_night_no_collector_result_publishes_nothing_and_the_slot_check_alarms() -> (
    None
):
    rig = Rig(InMemoryNightlyGateway(run=None, reading=None, previous=None))
    collected = rig.collect()
    result = rig.publisher().handle(rig.request(collected))
    assert result.status is EnumPublishStatus.NO_COLLECTOR_RESULT
    assert result.record is None
    assert rig.gateway.check_runs == []
    assert rig.records.rows == {}
    slot_check = HandlerCheckSlotResult(
        settings=_settings(),
        policy=POLICY,
        store=rig.alarms,
        receiver=rig.receiver,
    )
    after_lag = datetime.combine(
        SLOT, POLICY.nightly_slot_time_utc, tzinfo=UTC
    ) + timedelta(hours=POLICY.collector_max_lag_hours, minutes=1)
    alarmed = slot_check.handle(ModelSlotCheckRequest(slot_date=SLOT, now=after_lag))
    assert len(alarmed.alarms_created) == 1
    assert [a.kind for a, _ in rig.alarms.rows.values()] == [
        EnumAlarmKind.NO_COLLECTOR_RESULT
    ]


def test_record_incomplete_night_an_unreadable_night_publishes_nothing() -> None:
    rig = Rig(_night(fail_reads=True))
    result = rig.publish()
    assert result.status is EnumPublishStatus.NO_COLLECTOR_RESULT
    assert rig.gateway.check_runs == []


def test_record_incomplete_night_a_result_that_is_not_collected_publishes_nothing() -> (
    None
):
    rig = Rig()
    collected = rig.collect()
    rig.alarms.record_collector_result(REPO, SLOT)
    other = Rig(InMemoryNightlyGateway(run=None, reading=None, previous=None))
    not_collected = other.collect()
    result = rig.publisher().handle(rig.request(not_collected))
    assert result.status is EnumPublishStatus.NOT_COLLECTED
    assert rig.gateway.check_runs == []
    assert collected.attribution is not None


def test_record_incomplete_night_a_disabled_collector_publishes_nothing() -> None:
    rig = Rig(collector_enabled=False)
    result = rig.publish()
    assert result.status is EnumPublishStatus.DISABLED
    assert rig.gateway.check_runs == []


# --- binding errors -----------------------------------------------------------------------------


def test_record_binding_a_collect_result_of_another_night_is_refused() -> None:
    rig = Rig()
    collected = rig.collect()
    with pytest.raises(RecordBindingError):
        rig.publisher().handle(
            rig.request(collected, slot_date=SLOT + timedelta(days=1))
        )
    assert rig.gateway.check_runs == []


def test_record_binding_a_tested_head_that_is_not_a_full_commit_id_is_refused() -> None:
    rig = Rig(_night(head="head-today"))
    with pytest.raises(RecordBindingError):
        rig.publish()
    assert rig.gateway.check_runs == []


def test_record_binding_a_gateway_write_error_type_is_the_ports() -> None:
    assert issubclass(GatewayWriteError, Exception)
