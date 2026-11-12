# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the lab alarm's delivery (OMN-23906).

Golden: a subject that enters the alarm state is posted once and appended once. Failing controls: a repeat
of the same alarm delivers nothing; an OK clears the episode and a re-raise inside the window posts but
appends no second row. Error: a refused channel or ledger is a typed failure naming the subject, the ledger
row is kept and retried until it lands, and nothing that does not write moves state.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.events.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.inmemory_adapters import (
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.adapters.runtime_adapters import (
    ChannelFromLibrary,
    FileDeliveryState,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
    events_for,
    load_topics,
    parse_command,
    render_ledger_row,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_CONDITION = "consumer_group_lag"
_START = datetime(2026, 11, 12, 6, 0, tzinfo=UTC)


def _overlay() -> ModelDeliveryOverlay:
    return ModelDeliveryOverlay.model_validate(
        yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    )


class _Clock:
    def __init__(self) -> None:
        self.now = _START

    def __call__(self) -> datetime:
        return self.now


def _build(
    state: InMemoryDeliveryState | None = None,
) -> tuple[
    HandlerLabAlarmDeliver,
    InMemoryAlarmChannel,
    InMemoryAlarmLedger,
    InMemoryDeliveryState,
    _Clock,
]:
    channel, ledger, clock = InMemoryAlarmChannel(), InMemoryAlarmLedger(), _Clock()
    held = state or InMemoryDeliveryState()
    handler = HandlerLabAlarmDeliver(
        overlay=_overlay(), channel=channel, ledger=ledger, state=held, clock=clock
    )
    return handler, channel, ledger, held, clock


def _alarm(subject: str = "group-a") -> ModelLabAlarm:
    return ModelLabAlarm(
        condition=_CONDITION, subject=subject, detail="lag grew 3->9 | for 3 samples"
    )


def _request(
    outcome: EnumDeliveryCondition = EnumDeliveryCondition.ALARM,
    alarms: tuple[ModelLabAlarm, ...] | None = None,
    *,
    write: bool = True,
) -> ModelLabAlarmDeliveryRequest:
    if alarms is None:
        alarms = (_alarm(),) if outcome is EnumDeliveryCondition.ALARM else ()
    return ModelLabAlarmDeliveryRequest(
        correlation_id="c-1",
        condition=_CONDITION,
        outcome=outcome,
        alarms=alarms,
        evidence="2 groups read; growth of 1 or more",
        write=write,
    )


def test_new_alarm_posts_once_and_appends_one_row() -> None:
    handler, channel, ledger, _, _ = _build()
    result = handler.handle(_request())
    assert result.outcome is EnumDeliveryOutcome.DELIVERED
    [delivery] = result.deliveries
    assert delivery.key == f"{_CONDITION}:group-a"
    assert (delivery.channel, delivery.ledger) == (
        EnumDeliveryStep.DELIVERED,
        EnumDeliveryStep.DELIVERED,
    )
    [(category, text)] = channel.posts
    assert category == _overlay().channel.category
    assert "group-a" in text
    assert _CONDITION in text
    [(request_id, row)] = ledger.rows
    cells = row.split(" | ")
    overlay = _overlay().ledger
    assert cells[1:6] == [
        "FRICTION",
        f"lane={overlay.lane}",
        f"actor={overlay.actor}",
        "model=none",
        f"class={overlay.friction_class}",
    ]
    assert f"existing={overlay.ticket}" in cells
    assert f"cost={overlay.cost}" in cells
    assert f"req={request_id}" in cells
    assert "detail=lag grew 3->9 / for 3 samples" in cells
    assert sum(1 for cell in cells if cell.startswith("detail=")) == 1


def test_repeat_of_the_same_alarm_delivers_nothing() -> None:
    handler, channel, ledger, _, _ = _build()
    handler.handle(_request())
    again = handler.handle(_request())
    assert again.outcome is EnumDeliveryOutcome.NOTHING_NEW
    assert again.deliveries[0].channel is EnumDeliveryStep.SUPPRESSED
    assert len(channel.posts) == 1
    assert len(ledger.rows) == 1


def test_ok_clears_and_a_reraise_inside_the_window_posts_without_a_second_row() -> None:
    handler, channel, ledger, _, clock = _build()
    handler.handle(_request())
    cleared = handler.handle(_request(EnumDeliveryCondition.OK))
    assert cleared.cleared == (f"{_CONDITION}:group-a",)
    clock.now += timedelta(minutes=10)
    again = handler.handle(_request())
    assert again.deliveries[0].channel is EnumDeliveryStep.DELIVERED
    assert again.deliveries[0].ledger is EnumDeliveryStep.SUPPRESSED
    assert len(channel.posts) == 2
    assert len(ledger.rows) == 1


def test_a_reraise_after_the_window_appends_a_new_row() -> None:
    handler, _, ledger, _, clock = _build()
    handler.handle(_request())
    handler.handle(_request(EnumDeliveryCondition.OK))
    clock.now += timedelta(minutes=_overlay().ledger.dedupe_window_minutes + 1)
    handler.handle(_request())
    assert len(ledger.rows) == 2


def test_indeterminate_neither_clears_nor_starts_an_episode() -> None:
    handler, channel, ledger, _, _ = _build()
    handler.handle(_request())
    handler.handle(_request(EnumDeliveryCondition.INDETERMINATE))
    handler.handle(_request())
    assert len(channel.posts) == 1
    assert len(ledger.rows) == 1


def test_a_second_subject_of_a_standing_episode_is_new() -> None:
    handler, channel, ledger, _, _ = _build()
    handler.handle(_request())
    result = handler.handle(_request(alarms=(_alarm(), _alarm("group-b"))))
    assert [d.channel for d in result.deliveries] == [
        EnumDeliveryStep.SUPPRESSED,
        EnumDeliveryStep.DELIVERED,
    ]
    assert len(channel.posts) == 2
    assert len(ledger.rows) == 2


def test_the_golden_chain_ends_in_the_completed_event() -> None:
    handler, *_ = _build()
    chain = events_for(handler.handle(_request()), load_topics())
    topics = load_topics()
    assert [topic for topic, _ in chain] == [
        topics["delivery_started"],
        topics["delivery_alarm"],
        topics["delivery_completed"],
    ]
    assert (
        topics["delivery_completed"]
        == "onex.evt.omnimarket.lab-alarm-delivery-completed.v1"
    )


def test_a_refused_channel_is_a_typed_failure_and_is_not_resent() -> None:
    handler, channel, ledger, _, _ = _build()
    channel.refuse = "credentials unresolved"
    failed = handler.handle(_request())
    assert failed.outcome is EnumDeliveryOutcome.FAILED
    assert f"{_CONDITION}:group-a: channel: credentials unresolved" in failed.failure
    topics = load_topics()
    assert events_for(failed, topics)[-1][0] == topics["delivery_failed"]
    assert len(ledger.rows) == 1
    channel.refuse = None
    again = handler.handle(_request())
    assert channel.posts == []
    assert again.outcome is EnumDeliveryOutcome.NOTHING_NEW


def test_a_refused_ledger_row_is_kept_and_retried_until_it_lands() -> None:
    handler, channel, ledger, held, _ = _build()
    ledger.refuse = "ledger host unreachable"
    failed = handler.handle(_request())
    assert failed.outcome is EnumDeliveryOutcome.FAILED
    assert failed.deliveries[0].channel is EnumDeliveryStep.DELIVERED
    assert list(held.state["ledger_pending"]) == [f"{_CONDITION}:group-a"]
    ledger.refuse = None
    retry = handler.handle(_request())
    assert retry.retried == (f"{_CONDITION}:group-a",)
    assert retry.deliveries[0].ledger is EnumDeliveryStep.DELIVERED
    assert retry.outcome is EnumDeliveryOutcome.DELIVERED
    assert len(ledger.rows) == 1
    assert len(channel.posts) == 1
    assert held.state["ledger_pending"] == {}
    last = handler.handle(_request())
    assert last.outcome is EnumDeliveryOutcome.NOTHING_NEW
    assert len(ledger.rows) == 1


def test_a_ledger_that_already_holds_the_row_answers_duplicate() -> None:
    handler, _, ledger, _held, clock = _build()
    handler.handle(_request())
    request_id = ledger.rows[0][0]
    # The same episode redelivered after the local state was lost: the request id is the same.
    fresh = HandlerLabAlarmDeliver(
        overlay=_overlay(),
        channel=InMemoryAlarmChannel(),
        ledger=ledger,
        state=InMemoryDeliveryState(),
        clock=clock,
    )
    result = fresh.handle(_request())
    assert result.deliveries[0].ledger is EnumDeliveryStep.DUPLICATE
    assert result.deliveries[0].ledger_request_id == request_id
    assert len(ledger.rows) == 1


def test_a_request_that_does_not_write_delivers_nothing_and_keeps_nothing() -> None:
    handler, channel, ledger, held, _ = _build()
    result = handler.handle(_request(write=False))
    assert result.outcome is EnumDeliveryOutcome.SKIPPED
    assert not result.state_kept
    assert channel.posts == []
    assert ledger.rows == []
    assert held.state == {}


@pytest.mark.parametrize(
    "build",
    [
        lambda: ModelLabAlarmDeliveryRequest(
            correlation_id="c",
            condition=_CONDITION,
            outcome=EnumDeliveryCondition.ALARM,
            alarms=(ModelLabAlarm(condition="other", subject="s", detail="d"),),
        ),
        lambda: ModelLabAlarmDeliveryRequest(
            correlation_id="c",
            condition=_CONDITION,
            outcome=EnumDeliveryCondition.OK,
            alarms=(_alarm(),),
        ),
        lambda: ModelLabAlarm(condition=_CONDITION, subject="s", detail=""),
    ],
    ids=["other-condition", "alarms-on-ok", "blank-detail"],
)
def test_malformed_requests_are_refused(build: Callable[[], object]) -> None:
    with pytest.raises(ValidationError):
        build()


def test_parse_command_names_the_bad_field() -> None:
    assert parse_command(b'{"correlation_id": "c"}').startswith("bad-payload:")
    good = _request().model_dump_json().encode()
    assert isinstance(parse_command(good), ModelLabAlarmDeliveryRequest)


def test_the_ledger_row_cannot_forge_a_cell() -> None:
    row = render_ledger_row(
        condition=_CONDITION,
        subject="a | b\nc",
        detail="x | y",
        evidence="e" * 700,
        stamp=_START,
        raised_at="2026-11-12T06:00:00Z",
        request_id="00000000-0000-4000-8000-000000000001",
        settings=_overlay().ledger,
    )
    assert "\n" not in row
    cells = row.split(" | ")
    assert "subject=a / b c" in cells
    assert "detail=x / y" in cells
    evidence = next(c for c in cells if c.startswith("evidence="))
    assert len(evidence) == len("evidence=") + _overlay().ledger.evidence_limit
    assert evidence.endswith("…")


def test_the_file_state_round_trips_and_refuses_a_corrupt_file(tmp_path: Path) -> None:
    store = FileDeliveryState(tmp_path / "state" / "delivery.json")
    assert store.load() == {}
    store.save({"active": {"k": "2026-11-12T06:00:00Z"}})
    assert store.load() == {"active": {"k": "2026-11-12T06:00:00Z"}}
    (tmp_path / "state" / "delivery.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        store.load()


def test_a_channel_library_that_is_missing_is_a_failed_receipt(tmp_path: Path) -> None:
    receipt = ChannelFromLibrary(tmp_path / "gone.sh", 5).send(
        category="lab-alarm", text="x"
    )
    assert receipt.delivered is False
    assert "not found" in (receipt.failure or "")


def test_the_overlay_refuses_a_template_without_its_placeholders() -> None:
    body = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    body["channel"]["message_template"] = "{condition} only"
    with pytest.raises(ValidationError):
        ModelDeliveryOverlay.model_validate(body)
    body = yaml.safe_load((_NODE / "overlay.example.yaml").read_text(encoding="utf-8"))
    del body["ledger"]["dedupe_window_minutes"]
    with pytest.raises(ValidationError):
        ModelDeliveryOverlay.model_validate(body)
