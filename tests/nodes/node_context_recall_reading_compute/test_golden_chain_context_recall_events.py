# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of piece 16's reading (plan section 3.2, OMN-22980).

Publish the read command on the in-memory bus and assert every event it produces, in order,
under one correlation id, ending in the reading-completed event. The error chains inject a
defined fault: a bad payload, a handler that raises, a planted fact not recalled and an eval run
with no result. The last block turns each chain assertion red with a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_context_recall_reading_compute.handlers import (
    handler_context_recall_reading as module,
)
from omnimarket.nodes.node_context_recall_reading_compute.handlers.handler_context_recall_reading import (
    HandlerContextRecallReading,
    load_topics,
    publish_answer,
)
from tests.nodes.node_context_recall_reading_compute.reading_support import (
    eval_run,
    probe,
    request_data,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in ("probe_read", "eval_run_read", "reading_completed", "reading_failed")
]
CORRELATION = "chain-16"
Events = list[tuple[str, dict[str, Any]]]


async def _publish(handler: HandlerContextRecallReading, payload: bytes) -> Events:
    bus = EventBusInmemory()
    seen: Events = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        await publish_answer(bus, message.value, handler, TOPICS)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (TOPICS["reading_completed"], TOPICS["reading_failed"]):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["read_requested"], on_message=on_command, group_id="context-recall"
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["read_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def run(
    probes: list[dict[str, Any]] | None = None,
    eval_runs: list[dict[str, Any]] | None = None,
    handler: HandlerContextRecallReading | None = None,
    payload: bytes | None = None,
) -> Events:
    body = payload or json.dumps(request_data(probes, eval_runs)).encode()
    return asyncio.run(_publish(handler or HandlerContextRecallReading(), body))


def assert_golden_chain(events: Events) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["probe_read"],
        TOPICS["eval_run_read"],
        TOPICS["reading_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["recalled"] is True
    assert events[1][1]["ended_in_result"] is True
    assert events[-1][1]["verdict"] == "WORKING"
    assert events[-1][1]["reasons"] == []


def assert_flipped(events: Events, reason: str, read_topic: str) -> None:
    topics = [topic for topic, _ in events]
    assert topics[-1] == TOPICS["reading_completed"], topics
    assert read_topic in topics, topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert events[-1][1]["reasons"] == [reason]


def assert_typed_failure(events: Events, prefix: str) -> None:
    assert [topic for topic, _ in events] == [TOPICS["reading_failed"]]
    assert events[0][1]["failure"].startswith(prefix), events[0][1]


def test_golden_chain_planted_fact_recalled_and_eval_result_ends_in_working() -> None:
    assert_golden_chain(run())


def test_error_chain_planted_fact_not_recalled_ends_in_a_not_working_reading() -> None:
    events = run(probes=[probe(recalled_answer="nothing comes to mind")])
    assert_flipped(events, "planted-fact-not-recalled:p1", TOPICS["probe_read"])
    assert events[0][1]["recalled"] is False


def test_error_chain_eval_without_result_ends_in_a_not_working_reading() -> None:
    events = run(eval_runs=[eval_run(result_id=None)])
    assert_flipped(events, "eval-ended-without-result:e1", TOPICS["eval_run_read"])
    assert events[1][1]["ended_in_result"] is False


def test_error_chain_bad_payload_gives_a_typed_failure_event() -> None:
    assert_typed_failure(run(payload=b'{"correlation_id": "chain-16"}'), "bad-payload:")


def test_error_chain_not_json_gives_a_typed_failure_event() -> None:
    assert_typed_failure(run(payload=b"not json"), "bad-payload:")


def test_error_chain_handler_that_raises_gives_a_typed_failure_event() -> None:
    class Broken(HandlerContextRecallReading):
        def handle(self, request: Any) -> Any:
            raise RuntimeError("reader down")

    events = run(handler=Broken())
    assert_typed_failure(events, "handler-error:RuntimeError")
    assert events[0][1]["correlation_id"] == CORRELATION


# --- each chain assertion turns red on a named break --------------------------------------------

NAMED_MUTATIONS = {
    "recall-always-true": "fact_recalled answers True: golden stays green, the planted_fact error chain turns red",
    "eval-result-ignored": "eval_run_reason ignores a missing result: the eval_without_result chain turns red",
    "drops-probe-event": "answer_command drops the probe-read events: the golden chain turns red",
}


def _break(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    if name == "recall-always-true":
        monkeypatch.setattr(module, "fact_recalled", lambda *_args: True)
    elif name == "eval-result-ignored":
        monkeypatch.setattr(module, "eval_run_reason", lambda *_args: None)
    elif name == "drops-probe-event":
        original = module.events_of
        monkeypatch.setattr(
            module,
            "events_of",
            lambda reading, topics: [
                e for e in original(reading, topics) if e[0] != topics["probe_read"]
            ],
        )
    else:
        raise AssertionError(name)


def test_named_mutations_are_all_tested() -> None:
    assert set(NAMED_MUTATIONS) == {
        "recall-always-true",
        "eval-result-ignored",
        "drops-probe-event",
    }


def test_break_planted_fact_recall_always_true_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break(monkeypatch, "recall-always-true")
    events = run(probes=[probe(recalled_answer="nothing comes to mind")])
    with pytest.raises(AssertionError):
        assert_flipped(events, "planted-fact-not-recalled:p1", TOPICS["probe_read"])


def test_break_eval_without_result_ignored_turns_the_error_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break(monkeypatch, "eval-result-ignored")
    events = run(eval_runs=[eval_run(result_id=None)])
    with pytest.raises(AssertionError):
        assert_flipped(events, "eval-ended-without-result:e1", TOPICS["eval_run_read"])


def test_break_dropped_probe_event_turns_the_golden_chain_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _break(monkeypatch, "drops-probe-event")
    with pytest.raises(AssertionError):
        assert_golden_chain(run())
