# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the piece 11 reading run (plan section 3.2, OMN-22958).

Publish the input event on the in-memory bus and assert every event it produces, in order, under one
correlation id, ending in the run event; inject a defined fault and assert the typed failure event, the
ledger row and the NOT WORKING reading. The last block shows each chain assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.nodes.node_dev_deploy_reading_effect.adapters import (
    InMemoryDevDeployFacts,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.handler_dev_deploy_reading_run import (
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.models.model_dev_deploy_reading_run import (
    ModelDevDeployFacts,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_dev_deploy_reading_effect.dev_deploy_effect_support import (
    SHA_B,
    good_facts,
    handler,
    source,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "dev_deploy_check_passed",
        "dev_deploy_check_failed",
        "dev_deploy_reading_run_completed",
        "dev_deploy_reading_run_failed",
    )
]
CORRELATION = "chain-11"
PAYLOAD = json.dumps({"correlation_id": CORRELATION, "write": True})
Events = list[tuple[str, dict[str, object]]]


async def _publish(
    test_source: InMemoryDevDeployFacts, ledger: InMemoryLedger, payload: str
) -> Events:
    node = handler(test_source, ledger)
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
                TOPICS["dev_deploy_reading_run_completed"],
                TOPICS["dev_deploy_reading_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["dev_deploy_reading_requested"],
            on_message=on_command,
            group_id="dev-deploy-reading",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["dev_deploy_reading_requested"], key=None, value=payload.encode()
        )
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        await bus.close()
    return seen


def run(
    test_source: InMemoryDevDeployFacts, ledger: InMemoryLedger, payload: str = PAYLOAD
) -> Events:
    return asyncio.run(_publish(test_source, ledger, payload))


def assert_golden_chain(events: Events) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [TOPICS["dev_deploy_check_passed"]] * 2 + [
        TOPICS["dev_deploy_reading_run_completed"]
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert [body["rule"] for _, body in events[:2]] == ["receipts", "probe"]
    assert events[-1][1]["verdict"] == "WORKING"
    assert events[-1][1]["ledger"] == "written"


def assert_error_chain(events: Events, ledger: InMemoryLedger, rule: str) -> None:
    topics = [topic for topic, _ in events]
    assert TOPICS["dev_deploy_check_failed"] in topics, topics
    assert topics[-1] == TOPICS["dev_deploy_reading_run_completed"], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    failed = [b for t, b in events if t == TOPICS["dev_deploy_check_failed"]]
    assert [b["rule"] for b in failed] == [rule]
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert len(ledger.lines) == 1
    assert "verdict=NOT_WORKING" in ledger.lines[0]


def test_golden_chain_ends_in_a_working_reading_and_one_row() -> None:
    ledger = InMemoryLedger()
    events = run(source(), ledger)
    assert_golden_chain(events)
    assert len(ledger.lines) == 1
    assert "verdict=WORKING" in ledger.lines[0]


def test_error_chain_dropped_receipt_gives_a_failure_event_and_a_not_working_row() -> (
    None
):
    ledger = InMemoryLedger()
    events = run(source(good_facts().model_copy(update={"receipts": ()})), ledger)
    assert_error_chain(events, ledger, "receipts")
    assert "reason=no-pass-receipt:" in ledger.lines[0]


def test_error_chain_probe_that_saw_another_sha_gives_a_probe_failure_event() -> None:
    probe = good_facts().probes[0].model_copy(update={"observed_sha": SHA_B})
    ledger = InMemoryLedger()
    events = run(source(good_facts().model_copy(update={"probes": (probe,)})), ledger)
    assert_error_chain(events, ledger, "probe")
    assert "probe-sha-mismatch:" in ledger.lines[0]


def test_error_chain_missing_probe_gives_a_probe_failure_event() -> None:
    ledger = InMemoryLedger()
    events = run(source(good_facts().model_copy(update={"probes": ()})), ledger)
    assert_error_chain(events, ledger, "probe")
    assert "probe-missing" in ledger.lines[0]


def test_error_chain_unreadable_source_gives_an_unknown_reading_and_a_source_unreadable_row() -> (
    None
):
    facts = ModelDevDeployFacts(
        merges=good_facts().merges,
        receipts_unread_reason="lane-health projection unreadable: URLError",
        probes=good_facts().probes,
    )
    ledger = InMemoryLedger()
    events = run(source(facts), ledger)
    assert events[-1][0] == TOPICS["dev_deploy_reading_run_completed"]
    assert events[-1][1]["verdict"] == "UNKNOWN"
    assert "outcome=source-unreadable" in ledger.lines[0]


def test_error_chain_append_that_does_not_land_is_a_run_failure_event() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = run(source(), ledger)
    assert events[-1][0] == TOPICS["dev_deploy_reading_run_failed"]
    assert events[-1][1]["failure"] == "append-failed:refused"
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = run(source(), InMemoryLedger(), payload=json.dumps({"write": True}))
    assert [topic for topic, _ in events] == [TOPICS["dev_deploy_reading_run_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload:")


# -- each chain assertion can fail: the named breaks turn it red ------------------------------


def test_break_dropped_receipt_turns_the_golden_chain_red() -> None:
    events = run(
        source(good_facts().model_copy(update={"receipts": ()})), InMemoryLedger()
    )
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_break_dropped_terminal_event_turns_the_golden_chain_red() -> None:
    events = run(source(), InMemoryLedger())
    with pytest.raises(AssertionError):
        assert_golden_chain(events[:-1])


def test_break_success_in_place_of_failure_turns_the_error_chain_red() -> None:
    ledger = InMemoryLedger()
    events = run(source(), ledger)
    with pytest.raises(AssertionError):
        assert_error_chain(events, ledger, "receipts")


def test_break_wrong_rule_named_turns_the_error_chain_red() -> None:
    ledger = InMemoryLedger()
    events = run(source(good_facts().model_copy(update={"receipts": ()})), ledger)
    with pytest.raises(AssertionError):
        assert_error_chain(events, ledger, "probe")


def test_contract_declares_the_exact_dev_deploy_reading_event_topics() -> None:
    assert TOPICS == {
        "dev_deploy_reading_requested": "onex.cmd.omnimarket.dev-deploy-reading-requested.v1",
        "dev_deploy_check_passed": "onex.evt.omnimarket.dev-deploy-check-passed.v1",
        "dev_deploy_check_failed": "onex.evt.omnimarket.dev-deploy-check-failed.v1",
        "dev_deploy_reading_run_completed": "onex.evt.omnimarket.dev-deploy-reading-run-completed.v1",
        "dev_deploy_reading_run_failed": "onex.evt.omnimarket.dev-deploy-reading-run-failed.v1",
    }
