# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain event tests of the host table copies drift read (OMN-22809).

Inject a defined fault and assert the typed failure event and the reading on the ledger row.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_host_table_copies_drift_compute.drift_support import World
from tests.nodes.node_host_table_copies_drift_compute.test_golden_chain_host_table_copies_drift import (
    CORRELATION,
    PAYLOAD,
    TOPICS,
    publish,
)

pytestmark = pytest.mark.unit


def assert_failed_chain(
    events: list[tuple[str, dict[str, Any]]], *, verdict: str, outcome: str
) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["host_table_copies_drift_failed"],
        TOPICS["host_table_copies_drift_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[0][1]["verdict"] == verdict
    assert events[0][1]["outcome"] == outcome
    assert events[-1][1]["failed"] == 1


def test_error_chain_unreadable_copy_gives_a_failure_event_and_unknown_row() -> None:
    world = World()
    world.break_make_a_copy_unreadable()
    ledger = InMemoryLedger()
    events = asyncio.run(publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert len(ledger.lines) == 1
    assert "verdict=UNKNOWN" in ledger.lines[0]
    assert "reason=copy-unreadable-overlays-file-missing" in ledger.lines[0]


def test_error_chain_a_copy_that_is_not_a_host_table_gives_an_unknown_row() -> None:
    world = World()
    world.break_make_a_copy_not_a_table()
    ledger = InMemoryLedger()
    events = asyncio.run(publish(world, ledger, PAYLOAD))
    assert_failed_chain(events, verdict="UNKNOWN", outcome="source-unreadable")
    assert "reason=copy-unreadable-config-no-hosts-list" in ledger.lines[0]


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
        TOPICS["host_table_copies_drift_run_failed"]
    ]
    assert "bad-payload" in str(events[0][1]["failure"])


def test_error_chain_writing_run_without_appender_is_a_typed_failure() -> None:
    events = asyncio.run(publish(World(), None, PAYLOAD))
    assert [topic for topic, _ in events] == [
        TOPICS["host_table_copies_drift_run_failed"]
    ]
    assert events[0][1]["failure"] == "no-appender-for-a-writing-run"
