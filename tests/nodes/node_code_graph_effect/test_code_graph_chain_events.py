# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the code graph (plan section 3.2, OMN-22651).

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the success event; inject a defined fault and assert the typed failure event, the ledger
failure row and the NOT WORKING verdict. The last block shows the golden assertions turn red on a
named break: the contract links dropped from the handler's rules.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.models.model_code_graph import EnumGraphFault
from omnimarket.nodes.node_code_graph_effect.handlers.handler_code_graph import (
    HandlerCodeGraph,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_code_graph_effect.protocols import ProtocolRepoSource
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
    evidence_row_from_text,
)
from tests.nodes.node_code_graph_effect.code_graph_support import (
    HOST,
    RUN_AT,
    overlay,
    source,
)

pytestmark = pytest.mark.unit

TOPICS = load_topics()
EVENT_TOPICS = [
    TOPICS[role]
    for role in (
        "code_graph_run_started",
        "code_graph_repo_read",
        "code_graph_run_completed",
        "code_graph_run_failed",
    )
]
CORRELATION = "chain-1"


def _payload(fault: EnumGraphFault = EnumGraphFault.NONE) -> bytes:
    return json.dumps(
        {"correlation_id": CORRELATION, "write": True, "fault": fault.value}
    ).encode()


async def _publish(
    repos: ProtocolRepoSource, ledger: InMemoryLedger, payload: bytes
) -> list[tuple[str, dict[str, object]]]:
    handler = HandlerCodeGraph(
        overlay=overlay(),
        host=HOST,
        source=repos,
        appender=ledger,
        clock=lambda: RUN_AT,
    )
    bus = EventBusInmemory()
    seen: list[tuple[str, dict[str, object]]] = []
    done = asyncio.Event()

    async def on_command(message: Any) -> None:
        for topic, body in answer_command(message.value, handler, TOPICS):
            await bus.publish(topic, key=None, value=body)

    def collector(topic: str) -> Callable[[Any], Awaitable[None]]:
        async def collect(message: Any) -> None:
            seen.append((topic, json.loads(message.value)))
            if topic in (
                TOPICS["code_graph_run_completed"],
                TOPICS["code_graph_run_failed"],
            ):
                done.set()

        return collect

    await bus.start()
    try:
        await bus.subscribe(
            TOPICS["code_graph_requested"], on_message=on_command, group_id="code-graph"
        )
        for topic in EVENT_TOPICS:
            await bus.subscribe(
                topic, on_message=collector(topic), group_id=f"test-{topic}"
            )
        await bus.publish(TOPICS["code_graph_requested"], key=None, value=payload)
        await asyncio.wait_for(done.wait(), timeout=10)
    finally:
        await bus.close()
    return seen


def assert_golden_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics == [
        TOPICS["code_graph_run_started"],
        TOPICS["code_graph_repo_read"],
        TOPICS["code_graph_repo_read"],
        TOPICS["code_graph_run_completed"],
    ], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "WORKING", events[-1][1]
    assert events[-1][1]["ledger"] == "written"


def assert_failed_chain(events: list[tuple[str, dict[str, object]]]) -> None:
    topics = [topic for topic, _ in events]
    assert topics[0] == TOPICS["code_graph_run_started"], topics
    assert topics[-1] == TOPICS["code_graph_run_failed"], topics
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    assert str(events[-1][1]["failure"]).startswith("repo-unreadable")


@pytest.fixture(params=["memory", "git"])
def repos(request: pytest.FixtureRequest, tmp_path: Path) -> ProtocolRepoSource:
    return source(request.param, tmp_path)


def test_golden_chain(repos: ProtocolRepoSource) -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(repos, ledger, _payload()))
    assert_golden_chain(events)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["verdict"] == "WORKING"
    assert row.cells["correlation"] == CORRELATION


def test_error_chain_unreadable_repository(repos: ProtocolRepoSource) -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(
        _publish(repos, ledger, _payload(EnumGraphFault.UNREADABLE_REPO))
    )
    assert_failed_chain(events)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["verdict"] == "NOT_WORKING"
    assert row.cells["outcome"] == "source-unreadable"
    assert row.cells["drill"] == "yes"


def test_error_chain_bad_payload_is_a_typed_failure(repos: ProtocolRepoSource) -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(_publish(repos, ledger, b'{"write": true}'))
    assert [topic for topic, _ in events] == [TOPICS["code_graph_run_failed"]]
    assert str(events[0][1]["failure"]).startswith("bad-payload")
    assert ledger.lines == []


def test_break_dropped_contract_links_turns_the_golden_chain_red(
    repos: ProtocolRepoSource,
) -> None:
    """The named break: the contract links dropped. The golden assertions must fail on it."""
    ledger = InMemoryLedger()
    events = asyncio.run(
        _publish(repos, ledger, _payload(EnumGraphFault.DROP_CONTRACT_LINKS))
    )
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
    assert events[-1][1]["verdict"] == "NOT_WORKING"


def test_break_unreadable_repository_turns_the_golden_chain_red(
    repos: ProtocolRepoSource,
) -> None:
    ledger = InMemoryLedger()
    events = asyncio.run(
        _publish(repos, ledger, _payload(EnumGraphFault.UNREADABLE_REPO))
    )
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_contract_declares_the_exact_code_graph_event_topics() -> None:
    assert TOPICS["code_graph_requested"] == (
        "onex.cmd.omnimarket.code-graph-requested.v1"
    )
    assert TOPICS["code_graph_run_started"] == (
        "onex.evt.omnimarket.code-graph-run-started.v1"
    )
    assert TOPICS["code_graph_repo_read"] == (
        "onex.evt.omnimarket.code-graph-repo-read.v1"
    )
    assert TOPICS["code_graph_run_completed"] == (
        "onex.evt.omnimarket.code-graph-run-completed.v1"
    )
    assert TOPICS["code_graph_run_failed"] == (
        "onex.evt.omnimarket.code-graph-run-failed.v1"
    )
