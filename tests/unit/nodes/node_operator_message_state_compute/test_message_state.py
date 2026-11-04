# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Arrival/reply replay: exact linkage, input time and source order decide the state."""

from datetime import UTC, datetime
from importlib.resources import files

import pytest
import yaml

from omnimarket.models.model_operator_message_state import (
    ModelOperatorMessageStateRequest,
)
from omnimarket.nodes.node_operator_message_state_compute.handlers.handler_operator_message_state import (
    HandlerOperatorMessageState,
)

pytestmark = pytest.mark.unit
ARRIVAL = "2026-11-04T12:00:00Z | STATUS | lane=session-lane | kind=operator-message | capture=prompt-a | item=message-a | owner=orchestrator | arrived"
REPLY = "2026-11-04T12:01:00Z | MSG | from=orchestrator | to=operator | id=2026-11-04T12:01:00Z-orchestrator | answers=prompt-a | Here is the reply."


def run(rows: tuple[str, ...]):
    config = yaml.safe_load(
        files("omnimarket.nodes.node_operator_message_state_compute")
        .joinpath("contract.yaml")
        .read_text()
    )["config"]
    return HandlerOperatorMessageState().handle(
        ModelOperatorMessageStateRequest(
            rows=rows, now=datetime(2026, 11, 4, 14, tzinfo=UTC), config=config
        )
    )


def test_unanswered_arrival_has_owner_and_age() -> None:
    report = run((ARRIVAL,))
    item = report.items[0]
    assert item.state == "unanswered"
    assert item.owner_lane == "orchestrator"
    assert item.age_minutes == 120
    assert report.unanswered == (item.item_id,)


def test_explicit_reply_answers_only_its_arrival() -> None:
    other = ARRIVAL.replace("prompt-a", "prompt-b").replace("message-a", "message-b")
    report = run((ARRIVAL, other, REPLY))
    assert [i.state for i in report.items] == ["answered", "unanswered"]
    assert report.items[0].answered_at == datetime(2026, 11, 4, 12, 1, tzinfo=UTC)
    assert run((REPLY, other, ARRIVAL)) == report


def test_future_reply_and_lane_handoff_do_not_answer() -> None:
    future = REPLY.replace("2026-11-04", "2026-11-05")
    handoff = REPLY.replace("to=operator", "to=landing-controller")
    assert run((ARRIVAL, future, handoff)).items[0].state == "unanswered"


def test_same_stamp_reply_must_follow_arrival() -> None:
    reply = REPLY.replace("12:01:00Z", "12:00:00Z")
    assert run((reply, ARRIVAL)).items[0].state == "unanswered"
    assert run((ARRIVAL, reply)).items[0].state == "answered"


def test_replay_deduplicates_arrival_and_has_stable_id() -> None:
    arrival = ARRIVAL.replace(" | item=message-a", "")
    assert run((arrival, arrival)) == run((arrival,))
    assert run((arrival,)).items[0].item_id.startswith("message-")
