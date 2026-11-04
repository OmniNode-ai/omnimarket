# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 and AC5 (command shape): a stalled lane yields one stall event and one command."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_slack_publish_effect.models.model_slack_publish import (
    ModelSlackPublish,
)
from omnimarket.nodes.node_work_progress_alert_effect import NodeWorkProgressAlertEffect
from omnimarket.nodes.node_work_progress_alert_effect.models import (
    EnumWorkProgressOutcome,
    ModelLedgerRow,
    ModelWorkProgressAlertTrigger,
    ModelWorkProgressStalled,
)
from tests.nodes.node_work_progress_alert_effect.support import (
    CORRELATION,
    SETTINGS,
    Clock,
    Publisher,
    Reader,
    install_overlays,
    resolve_channel,
    row,
    stalled_lane,
)

SLACK = "onex.cmd.omnimarket.slack-publish.v1"
STALLED = "onex.evt.omnimarket.work-progress-stalled.v1"
KEY = "work-progress|lane:lane-a|placed|{row_id}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    install_overlays(monkeypatch, tmp_path)
    resolve_channel(monkeypatch)


def _node(
    rows: list[ModelLedgerRow], clock: Clock, publisher: Publisher
) -> NodeWorkProgressAlertEffect:
    return NodeWorkProgressAlertEffect(
        reader=Reader(rows), event_publisher=publisher, clock=clock
    )


def test_stalled_lane_yields_one_stall_event_and_one_command_with_one_key() -> None:
    rows = stalled_lane()
    publisher = Publisher()
    result = _node(rows, Clock(), publisher).handle(
        ModelWorkProgressAlertTrigger(correlation_id=CORRELATION)
    )
    key = KEY.format(row_id=rows[0].row_id)
    assert result.outcome is EnumWorkProgressOutcome.EVALUATED
    assert (result.subjects, result.findings, result.commands) == (1, 1, 1)
    [stalled] = publisher.on("work-progress-stalled.v1")
    [command] = publisher.on("slack-publish.v1")
    assert [topic for topic, _ in publisher.calls] == [STALLED, SLACK]
    assert stalled["idempotency_key"] == command["idempotency_key"] == key
    assert stalled["lane"] == "lane-a"
    assert stalled["phase"] == "placed"
    assert stalled["owner"] == "orch-1"
    assert stalled["budget_seconds"] == 900
    assert stalled["spent_seconds"] == 6000
    assert command["channel"] == "C0TESTCHANNEL"
    assert command["text"] == stalled["text"]
    assert command["correlation_id"] == str(CORRELATION)
    assert result.deliveries[0].idempotency_key == key
    ModelWorkProgressStalled.model_validate(stalled)


def test_command_validates_against_the_slack_nodes_input_model() -> None:
    publisher = Publisher()
    _node(stalled_lane(), Clock(), publisher).handle(ModelWorkProgressAlertTrigger())
    [command] = publisher.on("slack-publish.v1")
    model = ModelSlackPublish.model_validate(command)
    assert model.idempotency_key.startswith("work-progress|lane:lane-a|placed|")


def test_second_evaluation_repeats_the_same_key() -> None:
    clock, publisher = Clock(), Publisher()
    node = _node(stalled_lane(), clock, publisher)
    node.handle(ModelWorkProgressAlertTrigger())
    clock.advance(301)
    second = node.handle(ModelWorkProgressAlertTrigger())
    assert second.outcome is EnumWorkProgressOutcome.EVALUATED
    keys = [body["idempotency_key"] for _, body in publisher.calls]
    assert len(keys) == 4
    assert len(set(keys)) == 1


def test_heartbeat_inside_the_same_bucket_is_throttled() -> None:
    clock, publisher = Clock(), Publisher()
    reader = Reader(stalled_lane())
    node = NodeWorkProgressAlertEffect(
        reader=reader, event_publisher=publisher, clock=clock
    )
    node.handle(ModelWorkProgressAlertTrigger())
    clock.advance(10)
    second = node.handle(ModelWorkProgressAlertTrigger())
    assert second.outcome is EnumWorkProgressOutcome.THROTTLED
    assert len(reader.calls) == 1
    assert len(publisher.calls) == 2


def test_a_new_entering_row_gives_a_new_key() -> None:
    clock, publisher = Clock(), Publisher()
    claim = row(6000, "CLAIM", "lane-a", host="h051", parent="orch-1")
    reader = Reader([claim])
    node = NodeWorkProgressAlertEffect(
        reader=reader, event_publisher=publisher, clock=clock
    )
    node.handle(ModelWorkProgressAlertTrigger())
    # The lane moves on: a STATUS row enters claimed and has been there past 60 min.
    reader.rows = [claim, row(3700, "STATUS", "lane-a", note="working")]
    clock.advance(301)
    node.handle(ModelWorkProgressAlertTrigger())
    [first, second] = [b["idempotency_key"] for b in publisher.on("slack-publish.v1")]
    assert first != second
    assert str(second).startswith("work-progress|lane:lane-a|claimed|")


def test_lane_within_budget_emits_nothing_but_the_counts() -> None:
    publisher = Publisher()
    rows = [row(100, "CLAIM", "lane-a", host="h051")]
    result = _node(rows, Clock(), publisher).handle(ModelWorkProgressAlertTrigger())
    assert (result.subjects, result.findings, result.commands) == (1, 0, 0)
    assert publisher.calls == []


def test_unresolved_channel_is_recorded_on_the_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnimarket.nodes.node_work_progress_alert_effect.handlers import (
        handler_work_progress_alert,
    )

    monkeypatch.setattr(
        handler_work_progress_alert, "resolve_api_key_loop_safe", lambda *_a, **_k: None
    )
    publisher = Publisher()
    result = _node(stalled_lane(), Clock(), publisher).handle(
        ModelWorkProgressAlertTrigger()
    )
    assert result.commands == 0
    assert "resolved to None" in (result.deliveries[0].error or "")
    assert publisher.on("slack-publish.v1") == []


def test_no_injected_publisher_is_recorded_not_raised() -> None:
    node = NodeWorkProgressAlertEffect(reader=Reader(stalled_lane()), clock=Clock())
    result = node.handle(ModelWorkProgressAlertTrigger())
    assert result.findings == 1
    assert result.commands == 0
    assert "no event_publisher" in (result.deliveries[0].error or "")


def test_raising_the_interval_on_a_live_node_does_not_freeze_the_throttle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The interval is read from the overlay at every evaluation; raising it from
    # 300 to 900 must not leave the stored throttle state ahead of the clock.
    clock, publisher = Clock(), Publisher()
    reader = Reader(stalled_lane())
    node = NodeWorkProgressAlertEffect(
        reader=reader, event_publisher=publisher, clock=clock
    )
    assert (
        node.handle(ModelWorkProgressAlertTrigger()).outcome
        is EnumWorkProgressOutcome.EVALUATED
    )
    install_overlays(
        monkeypatch, tmp_path, settings={**SETTINGS, "check_interval_seconds": 900}
    )
    # Inside the new, longer interval the node is still throttled.
    clock.advance(400)
    inside = node.handle(ModelWorkProgressAlertTrigger())
    assert inside.outcome is EnumWorkProgressOutcome.THROTTLED
    assert inside.check_interval_seconds == 900
    # After the new interval has elapsed it evaluates and emits again.
    clock.advance(901)
    after = node.handle(ModelWorkProgressAlertTrigger())
    assert after.outcome is EnumWorkProgressOutcome.EVALUATED
    assert (after.findings, after.commands) == (1, 1)
    assert len(publisher.on("work-progress-stalled.v1")) == 2
    assert len(publisher.on("slack-publish.v1")) == 2
    # And it keeps evaluating on the new interval, not once.
    clock.advance(901)
    again = node.handle(ModelWorkProgressAlertTrigger())
    assert again.outcome is EnumWorkProgressOutcome.EVALUATED
    assert len(publisher.on("slack-publish.v1")) == 3


def test_lowering_the_interval_on_a_live_node_does_not_double_emit_in_one_interval(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clock, publisher = Clock(), Publisher()
    install_overlays(
        monkeypatch, tmp_path, settings={**SETTINGS, "check_interval_seconds": 900}
    )
    node = NodeWorkProgressAlertEffect(
        reader=Reader(stalled_lane()), event_publisher=publisher, clock=clock
    )
    node.handle(ModelWorkProgressAlertTrigger())
    install_overlays(
        monkeypatch, tmp_path, settings={**SETTINGS, "check_interval_seconds": 300}
    )
    # Inside one (new, shorter) interval of the last evaluation: throttled, no emission.
    clock.advance(100)
    inside = node.handle(ModelWorkProgressAlertTrigger())
    assert inside.outcome is EnumWorkProgressOutcome.THROTTLED
    assert len(publisher.on("slack-publish.v1")) == 1
    # Once the new interval has elapsed since the last evaluation, one more emission.
    clock.advance(201)
    after = node.handle(ModelWorkProgressAlertTrigger())
    assert after.outcome is EnumWorkProgressOutcome.EVALUATED
    assert len(publisher.on("slack-publish.v1")) == 2
    clock.advance(100)
    assert (
        node.handle(ModelWorkProgressAlertTrigger()).outcome
        is EnumWorkProgressOutcome.THROTTLED
    )
    assert len(publisher.on("slack-publish.v1")) == 2
