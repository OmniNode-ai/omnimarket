# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the stability lane refresh (OMN-21613).

Publish the command on the in-memory bus and assert every event it produces, in order, under one correlation
id; inject a defined fault and assert the typed refusal or failure event. The last block shows each chain
assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_stability_lane_refresh_effect.adapters import RecordingRunner
from omnimarket.nodes.node_stability_lane_refresh_effect.handlers.handler_stability_lane_refresh import (
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_stability_lane_refresh_effect.models import ModelRunOutcome
from tests.nodes.node_stability_lane_refresh_effect.stability_support import (
    PIN,
    PIN_TEXT,
    Rig,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "stability_refresh_pin_accepted",
        "stability_refresh_refused",
        "stability_refresh_completed",
        "stability_refresh_failed",
    )
]
TERMINAL = {
    TOPICS["stability_refresh_refused"],
    TOPICS["stability_refresh_completed"],
    TOPICS["stability_refresh_failed"],
}
CORRELATION = "chain-21613"
Events = list[tuple[str, dict[str, object]]]


def payload(**changes: object) -> str:
    body: dict[str, object] = {"correlation_id": CORRELATION, "execute": True}
    body.update(changes)
    return json.dumps(body)


async def _publish(rig: Rig, text: str) -> Events:
    bus = EventBusInmemory()
    seen: Events = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, rig.handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in TERMINAL:
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["stability_refresh_requested"],
            on_message=on_command,
            group_id="stability-refresh",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        await bus.publish(
            TOPICS["stability_refresh_requested"], key=None, value=text.encode()
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def run(rig: Rig, text: str | None = None) -> Events:
    return asyncio.run(_publish(rig, text or payload()))


def assert_golden_chain(events: Events) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["stability_refresh_pin_accepted"],
        TOPICS["stability_refresh_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["tags"] == dict(PIN.as_pairs())
    assert events[-1][1]["outcome"] == "refreshed"


def assert_refusal_chain(events: Events, outcome: str) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["stability_refresh_refused"]], topics
    assert events[0][1]["correlation_id"] == CORRELATION
    assert events[0][1]["outcome"] == outcome


def test_golden_chain_accepts_the_pin_and_completes_a_refresh() -> None:
    rig = Rig()
    events = run(rig)
    assert_golden_chain(events)
    assert len(rig.runner.executed) == 1


def test_golden_chain_for_the_set_already_running_completes_without_a_run() -> None:
    rig = Rig(applied=PIN)
    events = run(rig)
    assert events[-1][0] == TOPICS["stability_refresh_completed"]
    assert events[-1][1]["outcome"] == "already-pinned"
    assert rig.runner.calls == []


def test_error_chain_dev_tag_in_the_pin_is_a_refusal_and_runs_nothing() -> None:
    rig = Rig(pin_text=PIN_TEXT.replace("v3.4.5", "dev-20261030195247-1e3c83494587"))
    events = run(rig)
    assert_refusal_chain(events, "refused-not-release-tag")
    assert rig.runner.calls == []


def test_error_chain_requested_set_without_bump_is_a_refusal() -> None:
    rig = Rig()
    requested = {
        "omnibase_infra": "v1.2.4",
        "omnibase_core": "v2.3.4",
        "omnimarket": "v3.4.6",
    }
    events = run(rig, payload(requested=requested))
    assert_refusal_chain(events, "refused-pin-differs")
    assert rig.runner.calls == []


def test_error_chain_refresh_that_fails_ends_in_a_failed_event() -> None:
    rig = Rig(runner=RecordingRunner(ModelRunOutcome(return_code=2, output_tail="x")))
    events = run(rig)
    assert [t for t, _ in events] == [
        TOPICS["stability_refresh_pin_accepted"],
        TOPICS["stability_refresh_failed"],
    ]
    assert events[-1][1]["exit_code"] == 2
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = run(Rig(), json.dumps({"execute": True}))
    assert [t for t, _ in events] == [TOPICS["stability_refresh_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


# -- each chain assertion can fail: the named breaks turn it red ------------------------------


def test_break_refusal_in_place_of_a_refresh_turns_the_golden_chain_red() -> None:
    events = run(Rig(pin_text=PIN_TEXT.replace("v3.4.5", "main")))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_break_dropped_terminal_event_turns_the_golden_chain_red() -> None:
    events = run(Rig())
    with pytest.raises(AssertionError):
        assert_golden_chain(events[:-1])


def test_break_success_in_place_of_refusal_turns_the_refusal_chain_red() -> None:
    events = run(Rig())
    with pytest.raises(AssertionError):
        assert_refusal_chain(events, "refused-pin-differs")


def test_break_wrong_refusal_named_turns_the_refusal_chain_red() -> None:
    events = run(Rig(pin_text=PIN_TEXT.replace("v3.4.5", "main")))
    with pytest.raises(AssertionError):
        assert_refusal_chain(events, "refused-pin-differs")


def test_contract_declares_the_exact_stability_refresh_event_topics() -> None:
    assert TOPICS == {
        "stability_refresh_requested": "onex.cmd.omnimarket.stability-refresh-requested.v1",
        "stability_refresh_pin_accepted": "onex.evt.omnimarket.stability-refresh-pin-accepted.v1",
        "stability_refresh_refused": "onex.evt.omnimarket.stability-refresh-refused.v1",
        "stability_refresh_completed": "onex.evt.omnimarket.stability-refresh-completed.v1",
        "stability_refresh_failed": "onex.evt.omnimarket.stability-refresh-failed.v1",
    }
