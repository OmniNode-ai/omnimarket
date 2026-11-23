# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The nightly miss collector: completeness, the dead-man check, the alarm's dedup and crash cases, the owner and a real miss."""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import pytest
import yaml

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryOutcome,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.models.test_selector_reading import (
    EnumAttributionClass,
    EnumFileOutcome,
    EnumSelectorReadingState,
    EnumShardConclusion,
    ModelAttributionCandidate,
    ModelCandidateRecord,
    ModelCandidateRun,
    ModelCandidateSelection,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.__main__ import (
    EXIT_REFUSED,
    OVERLAY_ENV,
    main,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.inmemory_adapters import (
    InMemoryAlarmReceiver,
    InMemoryAlarmStore,
    InMemoryNightlyGateway,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.adapters.routed_receiver import (
    RoutedAlarmReceiver,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.handlers import (
    HandlerCheckSlotResult,
    HandlerCollectNightly,
)
from omnimarket.nodes.node_test_selector_miss_collect_effect.models.model_miss_collect import (
    ANY_FILE,
    EnumAlarmKind,
    EnumCollectStatus,
    EnumSlotStatus,
    ModelCollectRequest,
    ModelMissCollectSettings,
    ModelNightlyAlarm,
    ModelNightlyRun,
    ModelNightReading,
    ModelPreviousNight,
    ModelSlotCheckRequest,
    OwnerRoleUnsetError,
    load_policy,
)

pytestmark = pytest.mark.unit

REPO = "example-org/example-repo"
SLOT = date(2026, 11, 21)
POLICY = load_policy()
RED = "tests/nodes/node_x/test_x.py"
MERGED = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
OVERLAY = {
    "repo": REPO,
    "workflow_file": "nightly-full-suite.yml",
    "owner_role": "example-test-selection-owner",
    "alarm_route": "test_selector_miss",
    "collector_enabled": True,
    "status_tick": "example-lab-status-tick",
}
FULL = ModelCandidateSelection(selected_paths=("tests/",), split_count=1)
SKIPS_RED = ModelCandidateSelection(
    selected_paths=("tests/nodes/node_y/",), split_count=1
)
SELECTS_RED = ModelCandidateSelection(
    selected_paths=("tests/nodes/node_x/",), split_count=1
)
NOW = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)


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


def _candidate(
    sha: str, selection: ModelCandidateSelection
) -> ModelAttributionCandidate:
    return ModelAttributionCandidate(
        commit_sha=sha,
        pr_number=7,
        merged_at=MERGED,
        record=ModelCandidateRecord(checkout_sha=f"co-{sha}", executed=selection),
        run=ModelCandidateRun(
            run_id=5,
            attempt=1,
            checkout_sha=f"co-{sha}",
            completed_at=MERGED - timedelta(minutes=3),
            shard_conclusions={1: EnumShardConclusion.SUCCESS},
        ),
    )


def _gateway(
    *,
    red: bool = True,
    artifacts: int | None = None,
    in_error: int = 0,
    completed: bool = True,
    candidates: tuple[ModelAttributionCandidate, ...] = (),
    previous_passed: bool = True,
    fail_reads: bool = False,
) -> InMemoryNightlyGateway:
    return InMemoryNightlyGateway(
        run=ModelNightlyRun(run_id=100, completed=completed, head_sha="head-today"),
        reading=ModelNightReading(
            artifacts_found=POLICY.nightly_shard_count
            if artifacts is None
            else artifacts,
            shards_in_error=in_error,
            outcomes=_files(red),
        ),
        previous=ModelPreviousNight(
            head_sha="head-yesterday",
            outcomes=_files(red=not previous_passed) if red else _files(red=False),
        ),
        candidates=candidates,
        fail_reads=fail_reads,
    )


def _collector(
    gateway: InMemoryNightlyGateway,
    store: InMemoryAlarmStore,
    receiver: Any,
    **settings_over: Any,
) -> HandlerCollectNightly:
    return HandlerCollectNightly(
        settings=_settings(**settings_over),
        policy=POLICY,
        gateway=gateway,
        store=store,
        receiver=receiver,
    )


def _slot_check(
    store: InMemoryAlarmStore, receiver: Any, **settings_over: Any
) -> HandlerCheckSlotResult:
    return HandlerCheckSlotResult(
        settings=_settings(**settings_over),
        policy=POLICY,
        store=store,
        receiver=receiver,
    )


def _collect(handler: HandlerCollectNightly, now: datetime = NOW) -> Any:
    return handler.handle(ModelCollectRequest(slot_date=SLOT, now=now))


def _alarm_events(
    store: InMemoryAlarmStore, receiver: InMemoryAlarmReceiver
) -> tuple[int, int]:
    """(rows in the alarm table, alarms the receiver can see)."""
    return len(store.rows), len(receiver.received)


# --- completeness (AC3) ------------------------------------------------------------------------


def test_policy_shard_count_matches_the_nightly_workflow() -> None:
    workflow = (
        Path(__file__).resolve().parents[3] / ".github/workflows/nightly-full-suite.yml"
    ).read_text()
    declared = re.search(r'NIGHTLY_SPLIT_COUNT:\s*"(\d+)"', workflow)
    assert declared is not None
    assert POLICY.nightly_shard_count == int(declared.group(1))


def test_completeness_a_full_clean_night_raises_nothing() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    result = _collect(_collector(_gateway(red=False), store, receiver))
    assert result.status is EnumCollectStatus.COLLECTED
    assert result.attribution is not None
    assert result.attribution.complete
    assert _alarm_events(store, receiver) == (0, 0)
    assert store.has_collector_result(REPO, SLOT)


@pytest.mark.parametrize(
    "gateway_kwargs",
    [
        {"artifacts": 9},
        {"in_error": 1},
        {"red": False, "artifacts": 9},
    ],
)
def test_completeness_an_incomplete_night_raises_one_alarm(
    gateway_kwargs: dict[str, Any],
) -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    handler = _collector(_gateway(**gateway_kwargs), store, receiver)
    first = _collect(handler)
    _collect(handler)
    assert first.attribution is not None
    assert not first.attribution.complete
    assert first.attribution.reading_state is EnumSelectorReadingState.UNKNOWN
    assert _alarm_events(store, receiver) == (1, 1)
    alarm = next(iter(receiver.received.values()))
    assert alarm.kind is EnumAlarmKind.INCOMPLETE
    assert alarm.test_file == ANY_FILE
    assert alarm.owner_role == OVERLAY["owner_role"]


def test_completeness_a_run_not_terminal_waits_then_alarms_after_the_lag() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    handler = _collector(_gateway(completed=False), store, receiver)
    waiting = _collect(handler, NOW)
    assert waiting.status is EnumCollectStatus.WAITING
    assert _alarm_events(store, receiver) == (0, 0)
    late = _collect(handler, datetime(2026, 11, 21, 12, 0, tzinfo=UTC))
    assert late.attribution is not None
    assert not late.attribution.complete
    assert _alarm_events(store, receiver) == (1, 1)


def test_completeness_an_unreadable_night_is_not_clean_and_records_no_result() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    result = _collect(_collector(_gateway(fail_reads=True), store, receiver))
    assert result.status is EnumCollectStatus.READ_FAILED
    assert not store.has_collector_result(REPO, SLOT)
    late = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    check = _slot_check(store, receiver).handle(
        ModelSlotCheckRequest(slot_date=SLOT, now=late)
    )
    assert check.status is EnumSlotStatus.ALARMED
    assert _alarm_events(store, receiver) == (1, 1)


def test_completeness_no_collector_result_within_the_lag_raises_one_alarm() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    check = _slot_check(store, receiver)
    deadline = datetime.combine(
        SLOT, POLICY.nightly_slot_time_utc, tzinfo=UTC
    ) + timedelta(hours=POLICY.collector_max_lag_hours)
    before = check.handle(
        ModelSlotCheckRequest(slot_date=SLOT, now=deadline - timedelta(minutes=1))
    )
    assert before.status is EnumSlotStatus.WITHIN_LAG
    assert _alarm_events(store, receiver) == (0, 0)
    after = check.handle(
        ModelSlotCheckRequest(slot_date=SLOT, now=deadline + timedelta(minutes=1))
    )
    check.handle(
        ModelSlotCheckRequest(slot_date=SLOT, now=deadline + timedelta(hours=1))
    )
    assert after.status is EnumSlotStatus.ALARMED
    assert after.reading_state == "UNKNOWN"
    assert _alarm_events(store, receiver) == (1, 1)
    assert (
        next(iter(receiver.received.values())).kind is EnumAlarmKind.NO_COLLECTOR_RESULT
    )


def test_completeness_the_slot_check_raises_its_alarm_with_the_collector_disabled() -> (
    None
):
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    disabled = _collector(_gateway(), store, receiver, collector_enabled=False)
    assert _collect(disabled).status is EnumCollectStatus.DISABLED
    assert not store.has_collector_result(REPO, SLOT)
    late = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    check = _slot_check(store, receiver, collector_enabled=False)
    assert (
        check.handle(ModelSlotCheckRequest(slot_date=SLOT, now=late)).status
        is EnumSlotStatus.ALARMED
    )
    assert _alarm_events(store, receiver) == (1, 1)


def test_completeness_a_recorded_result_clears_the_slot_check() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    _collect(_collector(_gateway(red=False), store, receiver))
    late = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    result = _slot_check(store, receiver).handle(
        ModelSlotCheckRequest(slot_date=SLOT, now=late)
    )
    assert result.status is EnumSlotStatus.RESULT_RECORDED
    assert _alarm_events(store, receiver) == (0, 0)


# --- dedup and crash cases (AC4) -----------------------------------------------------------------


def _miss_gateway() -> InMemoryNightlyGateway:
    return _gateway(candidates=(_candidate("aaa", SKIPS_RED),))


def test_dedup_a_rerun_appends_no_second_alarm() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    handler = _collector(_miss_gateway(), store, receiver)
    first, second = _collect(handler), _collect(handler)
    assert len(first.alarms_created) == 1
    assert second.alarms_created == ()
    assert _alarm_events(store, receiver) == (1, 1)
    assert receiver.calls == 1


def test_dedup_two_concurrent_invocations_append_one() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    barrier = threading.Barrier(2)

    class Gate(InMemoryNightlyGateway):
        def read_night(self, repo: str, run: ModelNightlyRun) -> ModelNightReading:
            barrier.wait(timeout=10)
            return super().read_night(repo, run)

    gateway = Gate(
        run=ModelNightlyRun(run_id=100, completed=True, head_sha="head-today"),
        reading=ModelNightReading(
            artifacts_found=POLICY.nightly_shard_count,
            shards_in_error=0,
            outcomes=_files(True),
        ),
        previous=ModelPreviousNight(head_sha="head-yesterday", outcomes=_files(False)),
        candidates=(_candidate("aaa", SKIPS_RED),),
    )
    results: list[Any] = []
    handlers = [_collector(gateway, store, receiver) for _ in range(2)]
    threads = [
        threading.Thread(target=lambda h=h: results.append(_collect(h)))
        for h in handlers
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len(results) == 2
    assert sum(len(r.alarms_created) for r in results) == 1
    assert _alarm_events(store, receiver) == (1, 1)


def test_dedup_crash_after_the_upsert_and_before_delivery_leaves_one_delivered_alarm() -> (
    None
):
    store = InMemoryAlarmStore()
    receiver = InMemoryAlarmReceiver()

    class CrashBeforeAccept:
        armed = True

        def accept(self, alarm: ModelNightlyAlarm) -> Literal["accepted", "duplicate"]:
            if self.armed:
                self.armed = False
                raise Crash
            return receiver.accept(alarm)

    handler = _collector(_miss_gateway(), store, CrashBeforeAccept())
    with pytest.raises(Crash):
        _collect(handler)
    assert [status for _, status in store.rows.values()] == ["pending"]
    assert len(receiver.received) == 0
    _collect(handler)
    assert [status for _, status in store.rows.values()] == ["delivered"]
    assert _alarm_events(store, receiver) == (1, 1)


def test_dedup_crash_after_receipt_and_before_acknowledgment_leaves_one_receiver_visible_alarm() -> (
    None
):
    class CrashBeforeAck(InMemoryAlarmStore):
        armed = True

        def mark_delivered(self, key: str) -> None:
            if self.armed:
                self.armed = False
                raise Crash
            super().mark_delivered(key)

    store, receiver = CrashBeforeAck(), InMemoryAlarmReceiver()
    handler = _collector(_miss_gateway(), store, receiver)
    with pytest.raises(Crash):
        _collect(handler)
    assert len(receiver.received) == 1
    assert [status for _, status in store.rows.values()] == ["pending"]
    _collect(handler)
    assert receiver.calls == 2
    assert len(receiver.received) == 1
    assert [status for _, status in store.rows.values()] == ["delivered"]
    assert _alarm_events(store, receiver) == (1, 1)


def test_dedup_a_receiver_failure_leaves_the_alarm_pending_and_the_next_run_delivers_it() -> (
    None
):
    store, inner = InMemoryAlarmStore(), InMemoryAlarmReceiver()

    class Down:
        up = False

        def accept(self, alarm: ModelNightlyAlarm) -> Literal["accepted", "duplicate"]:
            if not self.up:
                raise OSError("receiver down")
            return inner.accept(alarm)

    receiver = Down()
    handler = _collector(_miss_gateway(), store, receiver)
    first = _collect(handler)
    assert len(first.delivery_failures) == 1
    assert _alarm_events(store, inner) == (1, 0)
    receiver.up = True
    second = _collect(handler)
    assert len(second.alarms_delivered) == 1
    assert _alarm_events(store, inner) == (1, 1)


def test_dedup_the_key_is_repo_slot_date_and_file_or_star() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    _collect(_collector(_miss_gateway(), store, receiver))
    assert list(receiver.received) == [f"{REPO}|{SLOT.isoformat()}|{RED}"]
    # The night's result is recorded, so the slot check adds no second alarm under the star key.
    late = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    _slot_check(store, receiver).handle(ModelSlotCheckRequest(slot_date=SLOT, now=late))
    assert list(receiver.received) == [f"{REPO}|{SLOT.isoformat()}|{RED}"]
    fresh_store, fresh_receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    _slot_check(fresh_store, fresh_receiver).handle(
        ModelSlotCheckRequest(slot_date=SLOT, now=late)
    )
    assert list(fresh_receiver.received) == [f"{REPO}|{SLOT.isoformat()}|{ANY_FILE}"]


# --- the owner (AC5) -----------------------------------------------------------------------------


@pytest.mark.parametrize("owner", [None, "", "   "])
def test_owner_unset_refuses_with_a_typed_error(owner: str | None) -> None:
    document = {k: v for k, v in OVERLAY.items() if k != "owner_role"}
    if owner is not None:
        document["owner_role"] = owner
    with pytest.raises(OwnerRoleUnsetError) as raised:
        ModelMissCollectSettings.load(document)
    assert raised.value.code == "owner_role_unset"


def test_owner_unset_the_start_exits_nonzero_with_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = tmp_path / "overlay.yaml"
    document = {k: v for k, v in OVERLAY.items() if k != "owner_role"}
    overlay.write_text(yaml.safe_dump(document))
    monkeypatch.setenv(OVERLAY_ENV, str(overlay))
    assert main() == EXIT_REFUSED
    assert json.loads(capsys.readouterr().err)["error"] == "owner_role_unset"


def test_owner_set_the_start_succeeds_and_the_alarm_carries_the_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(OVERLAY))
    monkeypatch.setenv(OVERLAY_ENV, str(overlay))
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["owner_role"] == OVERLAY["owner_role"]
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    _collect(_collector(_miss_gateway(), store, receiver))
    assert next(iter(receiver.received.values())).owner_role == OVERLAY["owner_role"]


def test_owner_blank_on_a_constructed_settings_refuses_the_handlers() -> None:
    blank = ModelMissCollectSettings.model_construct(**{**OVERLAY, "owner_role": " "})
    with pytest.raises(OwnerRoleUnsetError):
        HandlerCollectNightly(
            settings=blank,
            policy=POLICY,
            gateway=_gateway(),
            store=InMemoryAlarmStore(),
            receiver=InMemoryAlarmReceiver(),
        )
    with pytest.raises(OwnerRoleUnsetError):
        HandlerCheckSlotResult(
            settings=blank,
            policy=POLICY,
            store=InMemoryAlarmStore(),
            receiver=InMemoryAlarmReceiver(),
        )


# --- the injected miss (AC6) ---------------------------------------------------------------------


def test_injected_miss_is_classed_miss_and_raises_one_alarm() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    handler = _collector(
        _gateway(candidates=(_candidate("aaa", SKIPS_RED),)), store, receiver
    )
    result = _collect(handler)
    _collect(handler)
    assert result.attribution is not None
    (item,) = result.attribution.attributions
    assert item.klass is EnumAttributionClass.MISS
    assert item.suspects == ("aaa",)
    assert result.attribution.reading_state is EnumSelectorReadingState.NOT_WORKING
    assert _alarm_events(store, receiver) == (1, 1)
    alarm = next(iter(receiver.received.values()))
    assert alarm.kind is EnumAlarmKind.MISS
    assert "aaa" in alarm.text
    assert "kafka" in alarm.text


def test_injected_miss_the_same_fixture_with_the_test_selected_raises_none() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    result = _collect(
        _collector(
            _gateway(candidates=(_candidate("aaa", SELECTS_RED),)), store, receiver
        )
    )
    assert result.attribution is not None
    assert result.attribution.attributions[0].klass is EnumAttributionClass.INTERACTION
    assert _alarm_events(store, receiver) == (0, 0)


def test_unattributable_red_raises_one_alarm_and_preexisting_none() -> None:
    store, receiver = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    _collect(_collector(_gateway(candidates=()), store, receiver))
    assert _alarm_events(store, receiver) == (1, 1)
    assert next(iter(receiver.received.values())).kind is EnumAlarmKind.UNATTRIBUTABLE
    store2, receiver2 = InMemoryAlarmStore(), InMemoryAlarmReceiver()
    _collect(_collector(_gateway(previous_passed=False), store2, receiver2))
    assert _alarm_events(store2, receiver2) == (0, 0)


# --- the routed receiver and the contract ----------------------------------------------------------


class _FakeDelivery:
    def __init__(self, outcome: EnumDeliveryOutcome, failure: str = "") -> None:
        self.outcome, self.failure = outcome, failure
        self.requests: list[ModelLabAlarmDeliveryRequest] = []

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        self.requests.append(request)
        return ModelLabAlarmDeliveryResult(
            correlation_id=request.correlation_id,
            condition=request.condition,
            outcome=self.outcome,
            deliveries=(),
            cleared=(),
            retried=(),
            state_kept=True,
            failure=self.failure,
        )


def test_routed_receiver_maps_the_delivery_outcomes() -> None:
    alarm = ModelNightlyAlarm(
        repo=REPO,
        slot_date=SLOT,
        test_file=RED,
        kind=EnumAlarmKind.MISS,
        owner_role="r",
        route="test_selector_miss",
        text="t",
    )
    delivered = _FakeDelivery(EnumDeliveryOutcome.DELIVERED)
    assert RoutedAlarmReceiver(delivered).accept(alarm) == "accepted"
    assert delivered.requests[0].alarms[0].subject == alarm.key
    assert delivered.requests[0].condition == "test_selector_miss"
    assert (
        RoutedAlarmReceiver(_FakeDelivery(EnumDeliveryOutcome.NOTHING_NEW)).accept(
            alarm
        )
        == "duplicate"
    )
    with pytest.raises(OSError, match="boom"):
        RoutedAlarmReceiver(_FakeDelivery(EnumDeliveryOutcome.FAILED, "boom")).accept(
            alarm
        )


def test_the_contract_declares_its_operations_and_their_handlers() -> None:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[3]
            / "src/omnimarket/nodes/node_test_selector_miss_collect_effect/contract.yaml"
        ).read_text()
    )
    routes = {h["operation"]: h["handler"]["name"] for h in contract["cli_handlers"]}
    assert routes == {
        "collect_nightly": "HandlerCollectNightly",
        "check_slot_result": "HandlerCheckSlotResult",
        "publish_attribution_record": "HandlerPublishAttributionRecord",
    }
    assert {s["operation"] for s in contract["input_subscriptions"]} == set(routes)


def test_the_overlay_example_route_is_a_declared_alarm_source() -> None:
    root = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"
    overlay = yaml.safe_load(
        (
            root / "node_test_selector_miss_collect_effect/overlay.example.yaml"
        ).read_text()
    )
    sources = yaml.safe_load((root / "node_lab_alarm/contract.yaml").read_text())[
        "alarm_sources"
    ]
    declared = {s["condition"]: s for s in sources}
    assert overlay["alarm_route"] in declared
    assert declared[overlay["alarm_route"]]["posts_via"] == "deliver-effect"
