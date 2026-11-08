# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the gate store (OMN-22478, step 0 row 3).

Written by hand in the shape of plan section 3.2: publish the command on an in-memory bus,
assert every event it produces, in order, under the command's correlation id. The golden chain
reserves, consumes and releases; the error chains are a refusal the store answers by type, a
payload that is no request, and a store that faults mid-request (the state stays as it was).
The last block injects named breaks into the chain assertions and requires them to go red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_gate_store import EnumGateStoreOutcome
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store import (
    HandlerGateStore,
    answer_command,
    build_store,
    load_topics,
)
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from tests.nodes.node_gate_store_effect.contract_suite import POLICY, SuiteKillError

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = (TOPICS["gate_store_answered"], TOPICS["gate_store_failed"])

Answer = Callable[[bytes, HandlerGateStore, dict[str, str]], list[tuple[str, bytes]]]


def test_contract_topics_name_the_answered_and_failed_events() -> None:
    """The contract's roles resolve to the two events the chain tests below read."""
    assert TOPICS["gate_store_answered"] == "onex.evt.omnimarket.gate-store-answered.v1"
    assert TOPICS["gate_store_failed"] == "onex.evt.omnimarket.gate-store-failed.v1"


def command(op: str, key: str, **fields: object) -> bytes:
    body = {
        "correlation_id": f"chain-{key}",
        "operation": op,
        "step_key": key,
        **fields,
    }
    return json.dumps(body).encode()


async def publish(
    payloads: list[bytes],
    handler: HandlerGateStore,
    answer: Answer = answer_command,
) -> list[tuple[str, dict[str, Any]]]:
    """Publish each command on the in-memory bus; return every event produced, in order."""
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, Any]]] = []
    produced = 0

    async def on_command(message: Any) -> None:
        for topic, body in answer(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["gate_store_requested"], on_message=on_command, group_id="gate-store"
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        for payload in payloads:
            await bus.publish(TOPICS["gate_store_requested"], key=None, value=payload)
            produced += 1
            for _ in range(200):
                if len(seen) >= produced:
                    break
                await asyncio.sleep(0.005)
    finally:
        await bus.close()
    return seen


def assert_chain(
    events: list[tuple[str, dict[str, Any]]],
    expected: list[tuple[str, str, str | None]],
) -> None:
    """Each event, in order, is (topic, correlation id, outcome or failure prefix)."""
    assert len(events) == len(expected), (
        f"{len(events)} events, expected {len(expected)}"
    )
    for (topic, body), (want_topic, want_corr, want_what) in zip(
        events, expected, strict=True
    ):
        assert topic == want_topic, f"topic {topic}, expected {want_topic}"
        assert body["correlation_id"] == want_corr, "the correlation id was lost"
        got = body.get("outcome") or body.get("failure", "")
        assert want_what is None or str(got).startswith(want_what), (
            f"{got} != {want_what}"
        )


def fresh() -> HandlerGateStore:
    return HandlerGateStore(build_store(POLICY))


def golden_payloads() -> list[bytes]:
    return [
        command("reserve", "g1", piece="alpha", reservation_id="r1", now=0.0),
        command("consume", "g2", reservation_id="r1", now=1.0),
        command("release", "g3", reservation_id="r1", process_gone=True),
    ]


GOLDEN = [
    (TOPICS["gate_store_answered"], f"chain-g{i}", EnumGateStoreOutcome.OK.value)
    for i in (1, 2, 3)
]


def test_golden_chain_reserve_consume_release() -> None:
    events = asyncio.run(publish(golden_payloads(), fresh()))
    assert_chain(events, GOLDEN)
    assert [b["epoch"] for _, b in events] == [1, 2, 3], "each write advances the epoch"


def test_error_chain_refusal_is_an_answered_event() -> None:
    payloads = [
        command("reserve", f"e{i}", piece="gamma", reservation_id=f"x{i}", now=0.0)
        for i in (1, 2)
    ]
    events = asyncio.run(publish(payloads, fresh()))
    assert_chain(
        events,
        [
            (TOPICS["gate_store_answered"], "chain-e1", "ok"),
            (TOPICS["gate_store_answered"], "chain-e2", "refused_limit"),
        ],
    )


def test_error_chain_bad_payload_is_a_failure_event() -> None:
    events = asyncio.run(publish([b'{"operation": "reserve"}'], fresh()))
    assert_chain(events, [(TOPICS["gate_store_failed"], "unknown", "bad-payload")])


def test_error_chain_store_fault_is_a_failure_event_and_leaves_no_trace() -> None:
    armed = {"on": True}

    def hook(stage: str) -> None:
        if armed["on"] and stage == "before-commit":
            raise SuiteKillError(stage)

    store = InMemoryGateStore(POLICY, hook)
    handler = HandlerGateStore(store)
    payload = command("reserve", "f1", piece="alpha", reservation_id="r1", now=0.0)
    events = asyncio.run(publish([payload], handler))
    assert_chain(
        events,
        [(TOPICS["gate_store_failed"], "chain-f1", "store-fault:SuiteKillError")],
    )
    armed["on"] = False
    replay = asyncio.run(publish([payload], handler))
    assert_chain(replay, [(TOPICS["gate_store_answered"], "chain-f1", "ok")])
    assert replay[0][1]["counted"] == 1


# --- named breaks: each must turn the chain assertions red -------------------------------------


def drop_terminal(
    payload: bytes, handler: HandlerGateStore, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    return []


def wrong_correlation(
    payload: bytes, handler: HandlerGateStore, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    out = answer_command(payload, handler, topics)
    return [(t, b.replace(b"chain-", b"other-")) for t, b in out]


def failure_on_wrong_topic(
    payload: bytes, handler: HandlerGateStore, topics: dict[str, str]
) -> list[tuple[str, bytes]]:
    out = answer_command(payload, handler, topics)
    return [(topics["gate_store_failed"], b) for _, b in out]


@pytest.mark.parametrize(
    "broken",
    [drop_terminal, wrong_correlation, failure_on_wrong_topic],
    ids=["drop_terminal_event", "wrong_correlation_id", "answer_on_failure_topic"],
)
def test_named_break_turns_the_golden_chain_red(broken: Answer) -> None:
    events = asyncio.run(publish(golden_payloads(), fresh(), answer=broken))
    with pytest.raises(AssertionError):
        assert_chain(events, GOLDEN)
