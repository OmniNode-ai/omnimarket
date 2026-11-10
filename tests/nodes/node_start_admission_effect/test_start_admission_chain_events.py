# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the start-admission effect (OMN-22939).

Written by hand in the shape of plan section 3.2: publish the command on an in-memory bus, assert every event it
produces, in order, under the command's correlation id. The golden chains are a start the store admits and a start
the policy would refuse in observe mode (logged, started). The error chains are a payload that is no request, a
store that gives no answer by the deadline, and an enforce-mode refusal. The last test injects a named break into
the chain assertion and requires it to go red.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_gate_store import (
    EnumGateStoreOperation,
    ModelGateStoreRequest,
    ModelGateStoreResult,
)
from omnimarket.models.model_start_admission import EnumAdmissionMode
from omnimarket.nodes.node_gate_store_effect.handlers.store_in_memory import (
    InMemoryGateStore,
)
from omnimarket.nodes.node_start_admission_effect.handlers.adapter_log_in_memory import (
    InMemoryStartAdmissionLog,
)
from omnimarket.nodes.node_start_admission_effect.handlers.handler_start_admission import (
    HandlerStartAdmission,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_start_admission_effect.tests.test_start_admission import (
    STORE_POLICY,
    SilentStore,
    policy,
    request,
    stopped_store,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = (TOPICS["start_admission_answered"], TOPICS["start_admission_failed"])

Answer = Callable[
    [bytes, HandlerStartAdmission, dict[str, str]], list[tuple[str, bytes]]
]


def test_contract_topics_name_the_answered_and_failed_events() -> None:
    """The contract's roles resolve to the two events the chain tests below read."""
    assert (
        TOPICS["start_admission_answered"]
        == "onex.evt.omnimarket.start-admission-answered.v1"
    )
    assert (
        TOPICS["start_admission_failed"]
        == "onex.evt.omnimarket.start-admission-failed.v1"
    )


def command(key: str, outward: bool = False) -> bytes:
    return request(key, outward=outward).model_dump_json().encode()


async def publish(
    payloads: list[bytes],
    handler: HandlerStartAdmission,
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
            TOPICS["start_admission_requested"],
            on_message=on_command,
            group_id="start-admission",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"t-{topic}"
            )
        for payload in payloads:
            await bus.publish(
                TOPICS["start_admission_requested"], key=None, value=payload
            )
            produced += 1
            for _ in range(400):
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


def handler_for(
    store: InMemoryGateStore | SilentStore,
    mode: EnumAdmissionMode,
    deadline: float = 5.0,
) -> HandlerStartAdmission:
    return HandlerStartAdmission(
        store, InMemoryStartAdmissionLog(), policy(mode, deadline=deadline)
    )


def test_golden_chain_a_start_the_store_admits_is_answered_admitted() -> None:
    handler = handler_for(InMemoryGateStore(STORE_POLICY), EnumAdmissionMode.ENFORCE)
    events = asyncio.run(publish([command("g1")], handler))
    assert_chain(events, [(TOPICS["start_admission_answered"], "c-g1", "admitted")])
    assert events[0][1]["proceed"] is True


def test_golden_chain_observe_logs_what_it_would_refuse_and_the_start_proceeds() -> (
    None
):
    log = InMemoryStartAdmissionLog()
    handler = HandlerStartAdmission(
        stopped_store(), log, policy(EnumAdmissionMode.OBSERVE)
    )
    events = asyncio.run(publish([command("g2")], handler))
    assert_chain(
        events, [(TOPICS["start_admission_answered"], "c-g2", "observed_refusal")]
    )
    assert events[0][1]["proceed"] is True
    assert [row.correlation_id for row in log.rows()] == ["c-g2"], "one log row"


def test_error_chain_enforce_refuses_what_the_policy_refuses() -> None:
    handler = handler_for(stopped_store(), EnumAdmissionMode.ENFORCE)
    events = asyncio.run(publish([command("e1")], handler))
    assert_chain(events, [(TOPICS["start_admission_answered"], "c-e1", "refused")])
    assert events[0][1]["proceed"] is False


def test_error_chain_a_payload_that_is_no_request_is_a_typed_failure_event() -> None:
    handler = handler_for(InMemoryGateStore(STORE_POLICY), EnumAdmissionMode.ENFORCE)
    events = asyncio.run(publish([b'{"nope": 1}'], handler))
    assert_chain(events, [(TOPICS["start_admission_failed"], "unknown", "bad-payload")])


def test_error_chain_no_answer_by_the_deadline_proceeds_unadmitted_and_holds_outward() -> (
    None
):
    store = SilentStore()
    handler = handler_for(store, EnumAdmissionMode.ENFORCE, deadline=0.05)
    try:
        events = asyncio.run(
            publish([command("e3"), command("e4", outward=True)], handler)
        )
    finally:
        store.release.set()
    assert_chain(
        events,
        [
            (TOPICS["start_admission_answered"], "c-e3", "unadmitted"),
            (TOPICS["start_admission_answered"], "c-e4", "not_proceeded"),
        ],
    )
    assert [b["proceed"] for _, b in events] == [True, False]


def test_named_break_a_lost_correlation_id_turns_the_chain_red() -> None:
    """Injected break: the edge answers under another correlation id; the chain assertion must catch it."""

    def broken(
        payload: bytes, handler: HandlerStartAdmission, topics: dict[str, str]
    ) -> list[tuple[str, bytes]]:
        out = answer_command(payload, handler, topics)
        return [
            (t, json.dumps({**json.loads(b), "correlation_id": "lost"}).encode())
            for t, b in out
        ]

    handler = handler_for(InMemoryGateStore(STORE_POLICY), EnumAdmissionMode.ENFORCE)
    events = asyncio.run(publish([command("b1")], handler, answer=broken))
    with pytest.raises(AssertionError, match="correlation id was lost"):
        assert_chain(events, [(TOPICS["start_admission_answered"], "c-b1", "admitted")])


def test_named_break_a_store_that_faults_is_unadmitted_not_a_crash() -> None:
    """A store raising mid-request still yields exactly one answered event (no dropped command)."""

    class FaultStore:
        def transact(self, req: ModelGateStoreRequest) -> ModelGateStoreResult:
            assert req.operation is EnumGateStoreOperation.RESERVE
            raise RuntimeError("store down")

    handler = HandlerStartAdmission(
        FaultStore(), InMemoryStartAdmissionLog(), policy(EnumAdmissionMode.ENFORCE)
    )
    events = asyncio.run(publish([command("b2")], handler))
    assert_chain(events, [(TOPICS["start_admission_answered"], "c-b2", "unadmitted")])
