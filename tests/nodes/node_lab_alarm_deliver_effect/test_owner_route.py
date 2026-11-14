# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Owner-route tests of the lab alarm's delivery (OMN-23906).

Golden: an alarm whose condition the overlay routes to an owner posts one push notice under the route's
push category and appends exactly one MSG row addressed to the route's lane beside the FRICTION row.
Failing control: a condition with no route posts no push and appends no MSG row. Episode: a repeat of the
same alarm delivers nothing further, an OK ends the episode, a re-raise inside the window sends no owner
step and a re-raise after the window delivers a second MSG row. Error: a refused ledger keeps the owner
entry, reads FAILED, and the next request lands it without a second push; a refused push on the first
request is a FAILED owner step and is retried. Shape: the MSG row cannot be forged by a pipe or a newline
in the detail, and the overlay refuses a route whose template lacks a placeholder.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.models.lab_alarm_delivery import (
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
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver import (
    HandlerLabAlarmDeliver,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import ModelDeliveryOverlay

pytestmark = pytest.mark.unit

_NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_lab_alarm_deliver_effect"
)
_OWNER = "example_condition"  # the condition overlay.example.yaml routes to an owner
_UNROUTED = "consumer_group_lag"  # a condition with no route in overlay.example.yaml
_EVIDENCE = "2 groups read; growth of 1 or more"
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


def _alarm(subject: str = "group-a", condition: str = _OWNER) -> ModelLabAlarm:
    return ModelLabAlarm(
        condition=condition, subject=subject, detail="lag grew 3->9 | for 3 samples"
    )


def _request(
    condition: str = _OWNER,
    outcome: EnumDeliveryCondition = EnumDeliveryCondition.ALARM,
    alarms: tuple[ModelLabAlarm, ...] | None = None,
) -> ModelLabAlarmDeliveryRequest:
    if alarms is None:
        alarms = (
            (_alarm(condition=condition),)
            if outcome is EnumDeliveryCondition.ALARM
            else ()
        )
    return ModelLabAlarmDeliveryRequest(
        correlation_id="c-1",
        condition=condition,
        outcome=outcome,
        alarms=alarms,
        evidence=_EVIDENCE,
    )


def _row_kinds(ledger: InMemoryAlarmLedger) -> list[str]:
    """The appended rows' second cell: FRICTION for an alarm row, MSG for an owner row."""
    return [row.split(" | ")[1] for _, row in ledger.rows]


def _msg_rows(ledger: InMemoryAlarmLedger) -> list[tuple[str, str]]:
    """The appended MSG rows with their request ids, in append order."""
    return [(rid, row) for rid, row in ledger.rows if " | MSG | " in row]


def test_an_owner_routed_alarm_posts_one_push_and_appends_one_msg_row() -> None:
    handler, channel, ledger, _, _ = _build()
    overlay = _overlay()
    route = overlay.owner_routes[_OWNER]
    result = handler.handle(_request())
    assert result.outcome is EnumDeliveryOutcome.DELIVERED
    [delivery] = result.deliveries
    assert delivery.key == f"{_OWNER}:group-a"
    assert (delivery.channel, delivery.ledger) == (
        EnumDeliveryStep.DELIVERED,
        EnumDeliveryStep.DELIVERED,
    )
    assert delivery.owner is EnumDeliveryStep.DELIVERED
    assert [category for category, _ in channel.posts] == [
        overlay.channel.category,
        route.push_category,
    ]
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]
    push_category, push_text = channel.posts[1]
    assert push_category == route.push_category
    assert "group-a" in push_text
    assert _EVIDENCE in push_text
    [(msg_id, msg_row)] = _msg_rows(ledger)
    assert " | MSG | " in msg_row
    cells = msg_row.split(" | ")
    assert f"to={route.lane}" in cells
    assert f"ticket={route.ticket}" in cells
    assert _EVIDENCE in msg_row
    assert "lag grew 3->9 / for 3 samples" in msg_row
    assert f"req={delivery.owner_request_id}" in cells
    assert msg_id == delivery.owner_request_id


def test_a_condition_with_no_route_posts_no_push_and_appends_no_msg_row() -> None:
    handler, channel, ledger, _, _ = _build()
    result = handler.handle(_request(condition=_UNROUTED))
    assert result.outcome is EnumDeliveryOutcome.DELIVERED
    [delivery] = result.deliveries
    assert delivery.owner is EnumDeliveryStep.NOT_DUE
    assert delivery.owner_request_id == ""
    assert [category for category, _ in channel.posts] == [_overlay().channel.category]
    assert _row_kinds(ledger) == ["FRICTION"]
    assert _msg_rows(ledger) == []


def test_a_repeat_of_the_same_owner_routed_alarm_delivers_no_owner_step() -> None:
    handler, channel, ledger, _, _ = _build()
    handler.handle(_request())
    again = handler.handle(_request())
    assert again.outcome is EnumDeliveryOutcome.NOTHING_NEW
    [delivery] = again.deliveries
    assert delivery.channel is EnumDeliveryStep.SUPPRESSED
    assert delivery.ledger is EnumDeliveryStep.NOT_DUE
    assert delivery.owner is EnumDeliveryStep.NOT_DUE
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]
    assert len(channel.posts) == 2


def test_ok_ends_the_episode_and_a_reraise_inside_the_window_sends_no_owner_step() -> (
    None
):
    handler, channel, ledger, _, clock = _build()
    overlay = _overlay()
    handler.handle(_request())
    cleared = handler.handle(_request(outcome=EnumDeliveryCondition.OK))
    assert cleared.cleared == (f"{_OWNER}:group-a",)
    assert cleared.deliveries == ()
    clock.now += timedelta(minutes=10)
    again = handler.handle(_request())
    [delivery] = again.deliveries
    assert delivery.channel is EnumDeliveryStep.DELIVERED
    assert delivery.ledger is EnumDeliveryStep.SUPPRESSED
    assert delivery.owner is EnumDeliveryStep.SUPPRESSED
    assert [category for category, _ in channel.posts] == [
        overlay.channel.category,
        overlay.owner_routes[_OWNER].push_category,
        overlay.channel.category,
    ]
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]


def test_a_reraise_after_the_window_delivers_a_second_msg_row_and_push() -> None:
    handler, channel, ledger, _, clock = _build()
    overlay = _overlay()
    route = overlay.owner_routes[_OWNER]
    first = handler.handle(_request())
    handler.handle(_request(outcome=EnumDeliveryCondition.OK))
    clock.now += timedelta(minutes=overlay.ledger.dedupe_window_minutes + 1)
    again = handler.handle(_request())
    [delivery] = again.deliveries
    assert delivery.channel is EnumDeliveryStep.DELIVERED
    assert delivery.ledger is EnumDeliveryStep.DELIVERED
    assert delivery.owner is EnumDeliveryStep.DELIVERED
    assert delivery.owner_request_id != first.deliveries[0].owner_request_id
    assert [category for category, _ in channel.posts] == [
        overlay.channel.category,
        route.push_category,
        overlay.channel.category,
        route.push_category,
    ]
    assert _row_kinds(ledger) == ["FRICTION", "MSG", "FRICTION", "MSG"]
    msg_rows = _msg_rows(ledger)
    assert len(msg_rows) == 2
    assert msg_rows[0][0] == first.deliveries[0].owner_request_id
    assert msg_rows[1][0] == delivery.owner_request_id


def test_failure_a_refused_ledger_keeps_the_owner_entry_and_retries_without_a_second_push() -> (
    None
):
    handler, channel, ledger, held, _ = _build()
    overlay = _overlay()
    route = overlay.owner_routes[_OWNER]
    ledger.refuse = "ledger host unreachable"
    failed = handler.handle(_request())
    assert failed.outcome is EnumDeliveryOutcome.FAILED
    [delivery] = failed.deliveries
    assert delivery.channel is EnumDeliveryStep.DELIVERED
    assert delivery.ledger is EnumDeliveryStep.FAILED
    assert delivery.owner is EnumDeliveryStep.FAILED
    assert "ledger: ledger host unreachable" in delivery.failure
    assert list(held.state["ledger_pending"]) == [f"{_OWNER}:group-a"]
    assert list(held.state["owner_pending"]) == [f"{_OWNER}:group-a"]
    assert _row_kinds(ledger) == []
    ledger.refuse = None
    retry = handler.handle(_request())
    assert retry.outcome is EnumDeliveryOutcome.DELIVERED
    assert retry.retried == (f"{_OWNER}:group-a",)
    [retried_delivery] = retry.deliveries
    assert retried_delivery.ledger is EnumDeliveryStep.DELIVERED
    assert retried_delivery.owner is EnumDeliveryStep.DELIVERED
    assert retried_delivery.owner_request_id == delivery.owner_request_id
    assert [category for category, _ in channel.posts] == [
        overlay.channel.category,
        route.push_category,
    ]
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]
    assert "owner_pending" not in held.state
    assert list(held.state["owner_last"]) == [f"{_OWNER}:group-a"]
    last = handler.handle(_request())
    assert last.outcome is EnumDeliveryOutcome.NOTHING_NEW
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]


def test_failure_a_refused_push_on_the_first_request_is_a_failed_owner_step_and_is_retried() -> (
    None
):
    handler, channel, ledger, held, _ = _build()
    overlay = _overlay()
    route = overlay.owner_routes[_OWNER]
    channel.refuse = "credentials unresolved"
    failed = handler.handle(_request())
    assert failed.outcome is EnumDeliveryOutcome.FAILED
    [delivery] = failed.deliveries
    assert delivery.channel is EnumDeliveryStep.FAILED
    assert delivery.owner is EnumDeliveryStep.FAILED
    assert "channel: credentials unresolved" in delivery.failure
    assert channel.posts == []
    assert list(held.state["owner_pending"]) == [f"{_OWNER}:group-a"]
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]
    channel.refuse = None
    retry = handler.handle(_request())
    assert retry.outcome is EnumDeliveryOutcome.DELIVERED
    [retried_delivery] = retry.deliveries
    assert retried_delivery.owner is EnumDeliveryStep.DUPLICATE
    assert retried_delivery.owner_request_id == delivery.owner_request_id
    assert [category for category, _ in channel.posts] == [route.push_category]
    assert _row_kinds(ledger) == ["FRICTION", "MSG"]
    assert _msg_rows(ledger)[0][0] == retried_delivery.owner_request_id


def test_the_msg_row_cannot_be_forged_by_a_pipe_or_newline_in_the_detail() -> None:
    handler, _, ledger, _, _ = _build()
    overlay = _overlay()
    route = overlay.owner_routes[_OWNER]
    forged = ModelLabAlarm(
        condition=_OWNER,
        subject="group-a",
        detail="x | MSG | to=evil-lane\nrows are appended by me",
    )
    result = handler.handle(_request(alarms=(forged,)))
    [(request_id, msg_row)] = _msg_rows(ledger)
    assert request_id == result.deliveries[0].owner_request_id
    assert "\n" not in msg_row
    cells = msg_row.split(" | ")
    assert len(cells) == 8  # the real cells only; no pipe in the detail made another
    assert cells[1] == "MSG"
    assert cells.count("MSG") == 1
    assert sum(1 for cell in cells if cell.startswith("to=")) == 1
    assert cells[2] == f"from={overlay.ledger.lane}"
    assert f"to={route.lane}" in cells
    assert f"req={request_id}" in cells
    # The forged address survives only flattened inside the free text, not as a cell.
    assert cells[-1] == (
        "owner request: lab alarm example_condition on group-a: "
        "x / MSG / to=evil-lane rows are appended by me; "
        f"evidence: {_EVIDENCE}"
    )


def test_the_overlay_refuses_a_route_whose_template_lacks_a_placeholder() -> None:
    for field, template in (
        ("push_template", "lab alarm {condition} on {subject}: {detail}"),
        ("text_template", "owner request: {subject}: {detail}; evidence: {evidence}"),
    ):
        body = yaml.safe_load(
            (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
        )
        body["owner_routes"][_OWNER][field] = template
        with pytest.raises(ValidationError):
            ModelDeliveryOverlay.model_validate(body)
