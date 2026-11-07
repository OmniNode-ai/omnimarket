# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the shadow check (OMN-22902).

Publish the shadow command and assert every event it produces, in order, under one correlation id,
ending in the completed event; inject an unreadable pull request source and assert the typed failed
event and the ledger row. The last test shows the golden assertion turns red on a named break.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_code_graph import EnumGraphFault
from omnimarket.nodes.node_code_graph_effect.adapters.pull_request_sources import (
    InMemoryPullRequestSource,
)
from omnimarket.nodes.node_code_graph_effect.handlers.handler_shadow_check import (
    HandlerCodeGraphShadow,
    answer_shadow_command,
    load_shadow_topics,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import HOST, RUN_AT, overlay
from tests.unit.nodes.node_code_graph_effect.pr_support import (
    CLEAN_SHA,
    PLANTED,
    PLANTED_SHA,
    WIRED,
    memory_source,
    pull_request,
)

pytestmark = pytest.mark.unit

TOPICS = load_shadow_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "code_graph_shadow_pr_checked",
        "code_graph_shadow_completed",
        "code_graph_shadow_failed",
    )
]
CORRELATION = "shadow-chain-1"


def _payload(fault: EnumGraphFault = EnumGraphFault.NONE) -> bytes:
    return json.dumps(
        {"correlation_id": CORRELATION, "write": True, "fault": fault.value}
    ).encode()


async def _publish(
    pulls: InMemoryPullRequestSource, ledger: InMemoryLedger, payload: bytes
) -> list[tuple[str, dict[str, object]]]:
    handler = HandlerCodeGraphShadow(
        overlay=overlay(),
        host=HOST,
        source=memory_source({CLEAN_SHA: WIRED, PLANTED_SHA: PLANTED}),
        pull_requests=pulls,
        appender=ledger,
        clock=lambda: RUN_AT,
    )
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_shadow_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["code_graph_shadow_completed"],
                TOPICS["code_graph_shadow_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["code_graph_shadow_requested"],
            on_message=on_command,
            group_id="code-graph-shadow",
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(
            TOPICS["code_graph_shadow_requested"], key=None, value=payload
        )
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        await bus.close()
    return seen


def _pulls() -> InMemoryPullRequestSource:
    return InMemoryPullRequestSource(
        [pull_request(11, CLEAN_SHA), pull_request(12, PLANTED_SHA)]
    )


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["code_graph_shadow_pr_checked"],
        TOPICS["code_graph_shadow_pr_checked"],
        TOPICS["code_graph_shadow_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    final = events[-1][1]
    assert final["would_refuse"] == 1
    assert final["would_pass"] == 1
    assert final["holds_merge"] is False
    assert final["ledger"] == "written"


def test_golden_chain() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(_pulls(), ledger, _payload()))
    assert_golden_chain(events)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["would_refuse"] == "1"
    assert row.cells["correlation"] == CORRELATION


def test_error_chain_an_unreadable_source_ends_in_the_failed_event_and_a_row() -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(
        _publish(InMemoryPullRequestSource(error="state-gone"), ledger, _payload())
    )
    assert [topic for topic, _ in events] == [TOPICS["code_graph_shadow_failed"]]
    body = events[0][1]
    assert body["correlation_id"] == CORRELATION
    assert body["failure"] == "pull-requests-unreadable:state-gone"
    assert body["holds_merge"] is False
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["outcome"] == "source-unreadable"


def test_error_chain_a_payload_that_is_not_a_command_is_answered_not_dropped() -> None:
    events = asyncio.run(_publish(_pulls(), InMemoryLedger(), b'{"write": true}'))
    assert [topic for topic, _ in events] == [TOPICS["code_graph_shadow_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload")


def test_the_golden_assertion_turns_red_on_the_named_break() -> None:
    """Dropped contract links: the wired head reads as a would-refuse, so the golden counts break."""
    events = asyncio.run(
        _publish(
            _pulls(), InMemoryLedger(), _payload(EnumGraphFault.DROP_CONTRACT_LINKS)
        )
    )
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
