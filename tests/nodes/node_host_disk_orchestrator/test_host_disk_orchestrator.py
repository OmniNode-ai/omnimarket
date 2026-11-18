# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC4 (OMN-23866): schedule and low-disk event, one typed command per decision, results collected, run recorded."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from omnimarket.models.model_host_disk_decision import EnumHostDiskAlarmKind
from omnimarket.models.model_host_disk_orchestrator import (
    EnumHostDiskFailureReason,
    EnumHostDiskRunOutcome,
    ModelHostDiskEvictRequested,
    ModelHostDiskLowReported,
    ModelHostDiskRunCompleted,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.contract_config import (
    evict_requested_topic,
    run_completed_topic,
    slack_publish_topic,
)
from omnimarket.nodes.node_host_disk_orchestrator.handlers.model_slack_command import (
    ModelHostDiskSlackCommand,
)
from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)

from .orchestrator_inputs import (
    COMPLETED_TOPIC,
    FAILED_TOPIC,
    HOSTS,
    LOW_TOPIC,
    NODE_DIR,
    READ_TOPIC,
    REQUESTED_TOPIC,
    RUN_TOPIC,
    SLACK_TOPIC,
    T0,
    Orchestrator,
    completed_for,
    failed_for,
    payloads,
    read_event,
    tick,
)

pytestmark = pytest.mark.unit


def _ready() -> Orchestrator:
    o = Orchestrator()
    for host in HOSTS:
        o.read(read_event(host))
    return o


def test_the_contract_topics_are_the_topics_the_tests_and_the_effect_use() -> None:
    assert evict_requested_topic() == REQUESTED_TOPIC
    assert run_completed_topic() == RUN_TOPIC
    assert slack_publish_topic() == SLACK_TOPIC
    contract = yaml.safe_load((NODE_DIR / "contract.yaml").read_text())
    subscribed = set(contract["event_bus"]["subscribe_topics"])
    assert {READ_TOPIC, LOW_TOPIC, COMPLETED_TOPIC, FAILED_TOPIC} <= subscribed


def test_a_scheduled_run_sends_one_practice_command_per_decision_with_its_key() -> None:
    o = _ready()
    sent = payloads(o.scheduled.handle(tick(T0)))
    requests = [p for p in sent if isinstance(p, ModelHostDiskEvictRequested)]
    assert len(requests) == len(HOSTS)  # one uv_cache command per host
    assert all(r.practice_mode for r in requests)
    assert {r.idempotency_key for r in requests} == {
        f"{h}|uv_cache|{T0.isoformat()}" for h in HOSTS
    }
    assert all(r.command.idempotency_key == r.idempotency_key for r in requests)
    assert not any(isinstance(p, ModelHostDiskRunCompleted) for p in sent)  # still open


def test_results_collected_close_the_run_with_reading_decision_command_and_result() -> (
    None
):
    o = _ready()
    sent = payloads(o.scheduled.handle(tick(T0)))
    requests = [p for p in sent if isinstance(p, ModelHostDiskEvictRequested)]
    assert payloads(o.completed.handle(completed_for(requests[0]))) == []
    [record] = payloads(o.completed.handle(completed_for(requests[1])))
    assert isinstance(record, ModelHostDiskRunCompleted)
    assert record.outcome is EnumHostDiskRunOutcome.COMPLETED
    assert [r.host_name for r in record.readings] == list(HOSTS)
    assert record.decision is not None
    assert len(record.decision.commands) == 2
    assert [c.idempotency_key for c in record.commands] == [
        r.idempotency_key for r in requests
    ]
    assert [r.status for r in record.results] == ["completed", "completed"]
    assert record.practice_mode is True


def test_a_typed_failure_is_collected_not_dropped() -> None:
    o = _ready()
    requests = [
        p
        for p in payloads(o.scheduled.handle(tick(T0)))
        if isinstance(p, ModelHostDiskEvictRequested)
    ]
    o.completed.handle(completed_for(requests[0]))
    [record] = payloads(
        o.failed.handle(failed_for(requests[1], EnumHostDiskFailureReason.REFUSED))
    )
    assert record.outcome is EnumHostDiskRunOutcome.PARTIAL
    failed = [r for r in record.results if r.status == "failed"]
    assert [f.failure_reason for f in failed] == [EnumHostDiskFailureReason.REFUSED]


def test_a_command_with_no_result_by_its_deadline_is_recorded_as_such() -> None:
    o = _ready()
    requests = [
        p
        for p in payloads(o.scheduled.handle(tick(T0)))
        if isinstance(p, ModelHostDiskEvictRequested)
    ]
    o.completed.handle(completed_for(requests[0]))
    before_deadline = T0 + dt.timedelta(seconds=o.overlay.result_deadline_seconds - 1)
    assert payloads(o.scheduled.handle(tick(before_deadline))) == []
    after = T0 + dt.timedelta(seconds=o.overlay.result_deadline_seconds)
    [record] = payloads(o.scheduled.handle(tick(after)))
    assert record.outcome is EnumHostDiskRunOutcome.PARTIAL
    silent = [r for r in record.results if r.status == "failed"]
    assert [s.failure_reason for s in silent] == [
        EnumHostDiskFailureReason.NO_RESULT_BY_DEADLINE
    ]
    assert silent[0].idempotency_key == requests[1].idempotency_key


def test_a_tick_inside_the_interval_starts_nothing_and_a_low_disk_event_starts_a_run() -> (
    None
):
    o = _ready()
    o.scheduled.handle(tick(T0))
    inside = T0 + dt.timedelta(seconds=o.overlay.run_interval_seconds - 1)
    # nothing new: the earlier run is still open and its deadline has passed only at +300s, so close it first
    assert not any(
        isinstance(p, ModelHostDiskEvictRequested)
        for p in payloads(o.scheduled.handle(tick(inside)))
    )
    low = ModelHostDiskLowReported(host_name=HOSTS[0], reported_at=inside, free_bytes=1)
    sent = payloads(o.low.handle(low))
    assert any(isinstance(p, ModelHostDiskEvictRequested) for p in sent)


def test_a_result_for_no_open_run_changes_nothing() -> None:
    o = _ready()
    requests = [
        p
        for p in payloads(o.scheduled.handle(tick(T0)))
        if isinstance(p, ModelHostDiskEvictRequested)
    ]
    o.completed.handle(completed_for(requests[0]))
    o.completed.handle(completed_for(requests[1]))
    assert payloads(o.completed.handle(completed_for(requests[1]))) == []


def test_a_run_with_nothing_to_do_is_recorded_at_once() -> None:
    o = Orchestrator()
    for host in HOSTS:
        o.read(read_event(host, stale_revisions=0))
    [record] = [
        p
        for p in payloads(o.scheduled.handle(tick(T0)))
        if isinstance(p, ModelHostDiskRunCompleted)
    ]
    assert record.outcome is EnumHostDiskRunOutcome.NOTHING_DUE
    assert [r.host_name for r in record.readings] == list(HOSTS)


def test_the_alarm_goes_out_as_a_slack_publish_command_on_the_overlay_channel() -> None:
    o = Orchestrator()
    o.read(read_event(HOSTS[0], free_bytes=1))
    # host b has no reading: it alarms too
    sent = payloads(o.scheduled.handle(tick(T0)))
    alarms = [p for p in sent if isinstance(p, ModelHostDiskSlackCommand)]
    assert len(alarms) == 2
    for a in alarms:  # the mirror must stay wire-compatible with the real model
        ModelSlackPublish.model_validate(a.model_dump(mode="json"))
    assert {a.channel for a in alarms} == {o.overlay.alarm_channel}
    assert any(EnumHostDiskAlarmKind.UNDER_FLOOR.value in a.text for a in alarms)
    assert any(
        EnumHostDiskAlarmKind.MISSING_READING.value in (a.text or "") for a in alarms
    )
