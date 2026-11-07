# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the rotation readback (OMN-22913).

Publish the input event on the in-memory bus and assert every event it produces, in order,
under one correlation id, ending in the run event; inject a defined fault (a consumer held on
the old key, an unnamed approver, an append that does not land) and assert the typed failure
events. The last block shows each chain assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.adapters import (
    InMemoryConsumerCalls,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.handlers.handler_rotation_readback import (
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_secret_rotation_readback_effect.overlay import (
    ModelRotationReadbackOverlay,
)
from tests.unit.nodes.node_secret_reading_effect.rotation_support import (
    APPROVER,
    DEV_FOLDER,
    DEV_KEY,
    NEW_VALUE,
    OLD_VALUE,
    calls,
    handler,
    store,
)

pytestmark = pytest.mark.unit

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_rotation_readback_effect"
)
TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "rotation_consumer_passed",
        "rotation_consumer_failed",
        "rotation_refused",
        "rotation_readback_completed",
        "rotation_readback_failed",
    )
]
TERMINAL = (
    TOPICS["rotation_readback_completed"],
    TOPICS["rotation_readback_failed"],
    TOPICS["rotation_refused"],
)
CORRELATION = "chain-rot"
Events = list[tuple[str, dict[str, object]]]


def payload(**changes: object) -> str:
    body: dict[str, object] = {
        "correlation_id": CORRELATION,
        "rotation_id": "chain-rotation",
        "rotated_folder": DEV_FOLDER,
        "rotated_key": DEV_KEY,
        "approver": APPROVER,
        "drill": True,
        "write": True,
    }
    body.update(changes)
    return json.dumps(body)


async def _publish(
    test_store: InMemorySecretProbeStore,
    test_calls: InMemoryConsumerCalls,
    ledger: InMemoryLedger,
    message: str,
) -> Events:
    node = handler(test_store, test_calls, ledger)
    bus = EventBusInmemory()
    seen: Events = []
    done = asyncio.Event()

    async def on_command(received: Any) -> None:
        for topic, body in answer_command(received.value, node, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(received: Any) -> None:
            seen.append((topic, json.loads(received.value)))
            if topic in TERMINAL:
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["rotation_readback_requested"],
            on_message=on_command,
            group_id="rotation-readback",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["rotation_readback_requested"], key=None, value=message.encode()
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def run(
    test_store: InMemorySecretProbeStore,
    test_calls: InMemoryConsumerCalls,
    ledger: InMemoryLedger,
    message: str | None = None,
) -> Events:
    return asyncio.run(_publish(test_store, test_calls, ledger, message or payload()))


def assert_golden_chain(events: Events) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["rotation_consumer_passed"]] * 3 + [
        TOPICS["rotation_readback_completed"]
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "WORKING"
    assert events[-1][1]["ledger"] == "written"
    assert events[-1][1]["rows"] == 3


def assert_error_chain(events: Events, ledger: InMemoryLedger, consumer: str) -> None:
    topics = [topic for topic, _ in events]
    assert topics.count(TOPICS["rotation_consumer_failed"]) == 1, topics
    assert topics[-1] == TOPICS["rotation_readback_completed"], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    failed = [b for t, b in events if t == TOPICS["rotation_consumer_failed"]]
    assert [b["consumer"] for b in failed] == [consumer]
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert len(ledger.lines) == 3
    assert sum("verdict=NOT_WORKING" in line for line in ledger.lines) == 1


def test_golden_chain_ends_in_a_working_readback_and_one_row_per_consumer() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    events = run(test_store, calls(test_store), ledger)
    assert_golden_chain(events)
    assert len(ledger.lines) == 3


def test_error_chain_stale_consumer_gives_a_failure_event_naming_it_and_a_not_working_row() -> (
    None
):
    test_store = store()
    test_calls = calls(test_store)
    test_calls.hold("decider", OLD_VALUE)
    ledger = InMemoryLedger()
    events = run(test_store, test_calls, ledger)
    assert_error_chain(events, ledger, "decider")
    failed = next(b for t, b in events if t == TOPICS["rotation_consumer_failed"])
    assert failed["call"] == "rejected"


def test_error_chain_no_approver_is_one_refusal_event_and_no_row() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    events = run(test_store, calls(test_store), ledger, payload(approver=""))
    assert [t for t, _ in events] == [TOPICS["rotation_refused"]]
    assert events[0][1]["failure"] == "rotation-without-approver"
    assert events[0][1]["correlation_id"] == CORRELATION
    assert ledger.lines == []


def test_error_chain_append_that_does_not_land_is_a_run_failure_event() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = run(test_store, calls(test_store), ledger)
    assert events[-1][0] == TOPICS["rotation_readback_failed"]
    assert events[-1][1]["failure"] == "append-failed:refused"
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    test_store = store()
    events = run(
        test_store,
        calls(test_store),
        InMemoryLedger(),
        json.dumps({"write": True}),
    )
    assert [t for t, _ in events] == [TOPICS["rotation_readback_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


def test_an_event_never_carries_a_secret_value() -> None:
    test_store = store()
    test_calls = calls(test_store)
    test_calls.hold("decider", OLD_VALUE)
    text = json.dumps(run(test_store, test_calls, InMemoryLedger()))
    assert OLD_VALUE not in text
    assert NEW_VALUE not in text


# -- each chain assertion can fail: the named breaks turn it red ------------------------------


def test_break_stale_consumer_turns_the_golden_chain_red() -> None:
    test_store = store()
    test_calls = calls(test_store)
    test_calls.hold("estimator", OLD_VALUE)
    with pytest.raises(AssertionError):
        assert_golden_chain(run(test_store, test_calls, InMemoryLedger()))


def test_break_dropped_terminal_event_turns_the_golden_chain_red() -> None:
    test_store = store()
    events = run(test_store, calls(test_store), InMemoryLedger())
    with pytest.raises(AssertionError):
        assert_golden_chain(events[:-1])


def test_break_success_in_place_of_failure_turns_the_error_chain_red() -> None:
    test_store = store()
    ledger = InMemoryLedger()
    events = run(test_store, calls(test_store), ledger)
    with pytest.raises(AssertionError):
        assert_error_chain(events, ledger, "decider")


# -- the node's contract and both overlay profiles ---------------------------------------------


def test_contract_declares_the_exact_rotation_event_topics() -> None:
    assert TOPICS["rotation_readback_requested"] == (
        "onex.cmd.omnimarket.secret-rotation-readback-requested.v1"
    )
    assert TOPICS["rotation_consumer_passed"] == (
        "onex.evt.omnimarket.secret-rotation-consumer-passed.v1"
    )
    assert TOPICS["rotation_consumer_failed"] == (
        "onex.evt.omnimarket.secret-rotation-consumer-failed.v1"
    )
    assert TOPICS["rotation_refused"] == (
        "onex.evt.omnimarket.secret-rotation-refused.v1"
    )
    assert TOPICS["rotation_readback_completed"] == (
        "onex.evt.omnimarket.secret-rotation-readback-completed.v1"
    )
    assert TOPICS["rotation_readback_failed"] == (
        "onex.evt.omnimarket.secret-rotation-readback-failed.v1"
    )


@pytest.mark.parametrize(
    ("name", "backend", "command_backed"),
    [
        ("overlay.example.yaml", "inmemory", False),
        ("overlay.real.example.yaml", "secret-store-effect", True),
    ],
)
def test_both_overlay_profiles_load_and_pick_their_backend(
    name: str, backend: str, command_backed: bool
) -> None:
    import yaml

    overlay = ModelRotationReadbackOverlay.model_validate(
        yaml.safe_load((NODE / name).read_text(encoding="utf-8"))
    )
    assert overlay.store.backend_name == backend
    assert all(c.probe_command is not None for c in overlay.consumers) is command_backed
