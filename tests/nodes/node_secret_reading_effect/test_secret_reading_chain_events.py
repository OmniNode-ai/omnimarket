# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the piece 5 reading (plan section 3.2, OMN-22659).

Publish the input event on the in-memory bus and assert every event it produces, in order,
under one correlation id, ending in the run event; inject a defined fault and assert the typed
failure event, the ledger failure row and the NOT WORKING reading. The last block shows each
chain assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from omnimarket.nodes.node_secret_reading_effect.adapters import (
    InMemorySecretProbeStore,
)
from omnimarket.nodes.node_secret_reading_effect.handlers.handler_secret_reading import (
    answer_command,
    load_topics,
)
from tests.nodes.node_secret_reading_effect.secret_reading_support import (
    DECIDER_VALUE,
    GATE_VALUE,
    handler,
    store,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "secret_probe_passed",
        "secret_probe_failed",
        "secret_reading_run_completed",
        "secret_reading_run_failed",
    )
]
CORRELATION = "chain-5"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "consumers": [], "write": True})
Events = list[tuple[str, dict[str, object]]]


async def _publish(
    test_store: InMemorySecretProbeStore, ledger: InMemoryLedger, payload: str
) -> Events:
    node = handler(test_store, ledger)
    bus = EventBusInmemory()
    seen: Events = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, node, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["secret_reading_run_completed"],
                TOPICS["secret_reading_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["secret_reading_requested"],
            on_message=on_command,
            group_id="secret-reading",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["secret_reading_requested"], key=None, value=payload.encode()
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def run(
    test_store: InMemorySecretProbeStore,
    ledger: InMemoryLedger,
    payload: str = PAYLOAD,
) -> Events:
    return asyncio.run(_publish(test_store, ledger, payload))


def assert_golden_chain(events: Events) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["secret_probe_passed"]] * 2 + [
        TOPICS["secret_reading_run_completed"]
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "WORKING"
    assert events[-1][1]["ledger"] == "written"


def assert_error_chain(events: Events, ledger: InMemoryLedger, outcome: str) -> None:
    topics = [topic for topic, _ in events]
    assert TOPICS["secret_probe_failed"] in topics, topics
    assert topics[-1] == TOPICS["secret_reading_run_completed"], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    failed = [b for t, b in events if t == TOPICS["secret_probe_failed"]]
    assert [b["outcome"] for b in failed] == [outcome]
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert len(ledger.lines) == 1
    assert "verdict=NOT_WORKING" in ledger.lines[0]


def test_golden_chain_ends_in_a_working_reading_and_one_row() -> None:
    ledger = InMemoryLedger()
    events = run(store(), ledger)
    assert_golden_chain(events)
    assert len(ledger.lines) == 1
    assert "verdict=WORKING" in ledger.lines[0]


def test_error_chain_revoked_scope_gives_a_failure_event_and_a_not_working_row() -> (
    None
):
    test_store = store()
    test_store.revoke("gate")
    ledger = InMemoryLedger()
    events = run(test_store, ledger)
    assert_error_chain(events, ledger, "refused")
    assert "failed_consumers=gate:refused" in ledger.lines[0]


def test_error_chain_store_down_gives_failure_events_and_a_not_working_row() -> None:
    test_store = store()
    test_store.down = True
    ledger = InMemoryLedger()
    events = run(test_store, ledger)
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["secret_probe_failed"]] * 2 + [
        TOPICS["secret_reading_run_completed"]
    ]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert "verdict=NOT_WORKING" in ledger.lines[0]
    assert "outcome=source-unreadable" in ledger.lines[0]


def test_error_chain_append_that_does_not_land_is_a_run_failure_event() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = run(store(), ledger)
    assert events[-1][0] == TOPICS["secret_reading_run_failed"]
    assert events[-1][1]["failure"] == "append-failed:refused"
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = run(store(), InMemoryLedger(), payload=json.dumps({"write": True}))
    assert [topic for topic, _ in events] == [TOPICS["secret_reading_run_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


def test_an_event_never_carries_a_secret_value() -> None:
    text = json.dumps(run(store(), InMemoryLedger()))
    assert GATE_VALUE not in text
    assert DECIDER_VALUE not in text


# -- each chain assertion can fail: the named breaks turn it red ------------------------------


def test_break_revoked_scope_turns_the_golden_chain_red() -> None:
    test_store = store()
    test_store.revoke("decider")
    with pytest.raises(AssertionError):
        assert_golden_chain(run(test_store, InMemoryLedger()))


def test_break_dropped_terminal_event_turns_the_golden_chain_red() -> None:
    events = run(store(), InMemoryLedger())
    with pytest.raises(AssertionError):
        assert_golden_chain(events[:-1])


def test_break_success_in_place_of_failure_turns_the_error_chain_red() -> None:
    ledger = InMemoryLedger()
    events = run(store(), ledger)
    with pytest.raises(AssertionError):
        assert_error_chain(events, ledger, "refused")


def test_contract_declares_the_exact_secret_reading_event_topics() -> None:
    assert TOPICS["secret_reading_requested"] == (
        "onex.cmd.omnimarket.secret-reading-requested.v1"
    )
    assert TOPICS["secret_probe_passed"] == (
        "onex.evt.omnimarket.secret-probe-passed.v1"
    )
    assert TOPICS["secret_probe_failed"] == (
        "onex.evt.omnimarket.secret-probe-failed.v1"
    )
    assert TOPICS["secret_reading_run_completed"] == (
        "onex.evt.omnimarket.secret-reading-run-completed.v1"
    )
    assert TOPICS["secret_reading_run_failed"] == (
        "onex.evt.omnimarket.secret-reading-run-failed.v1"
    )
