# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the overlay consumer join check (OMN-23150).

Inject a defined fault and assert the typed failure event and the reading on the ledger row.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_overlay_drift_compute.tests.join_support import World
from omnimarket.nodes.node_overlay_drift_compute.tests.test_golden_chain_overlay_consumer_join import (
    CORRELATION,
    PAYLOAD,
    TOPICS,
    publish,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)

pytestmark = pytest.mark.unit


def assert_failed_chain(
    events: list[tuple[str, dict[str, Any]]], *, verdict: str, outcome: str
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["overlay_consumer_join_failed"],
        TOPICS["overlay_consumer_join_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["outcome"] == outcome
    assert events[-1][1]["failed"] == 1


def test_error_chain_unreadable_consumer_gives_a_failure_event_and_unknown_row() -> (
    None
):
    world = World()
    world.break_make_the_hook_unreadable()
    ledger = InMemoryLedger()
    events = asyncio.run(publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert len(ledger.lines) == 1
    assert "verdict=UNKNOWN" in ledger.lines[0]
    assert "unreadable=" in ledger.lines[0]
    assert "reason=source-unreadable-example-min-free" in ledger.lines[0]


def test_error_chain_overlay_key_that_is_gone_gives_an_unknown_row() -> None:
    world = World()
    world.break_remove_the_overlay_key()
    ledger = InMemoryLedger()
    events = asyncio.run(publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert events[0][1]["unreadable"] == ["example-min-free.overlay:key-not-found"]


def test_error_chain_append_that_does_not_land_is_a_failure_event() -> None:
    ledger = InMemoryLedger()
    ledger.append_status = "refused"
    events = asyncio.run(publish(World(), ledger, PAYLOAD))
    assert_failed_chain(events, verdict="WORKING", outcome="read")
    assert events[0][1]["ledger"] == "append-failed:refused"
    assert ledger.lines == []


def test_error_chain_bad_payload_is_answered_with_a_typed_failure() -> None:
    events = asyncio.run(publish(World(), InMemoryLedger(), b'{"write": "maybe"}'))
    assert [topic for topic, _ in events] == [
        TOPICS["overlay_consumer_join_run_failed"]
    ]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_writing_run_without_appender_is_a_typed_failure() -> None:
    events = asyncio.run(publish(World(), None, PAYLOAD))
    assert [topic for topic, _ in events] == [
        TOPICS["overlay_consumer_join_run_failed"]
    ]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"
