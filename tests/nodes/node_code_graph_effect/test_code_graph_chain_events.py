# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain event tests of the code graph (plan section 3.2, OMN-22651).

Publish the input event and assert every event it produces, in order, under one correlation id,
ending in the success event; inject a defined fault and assert the typed failure event, the ledger
failure row and the NOT WORKING verdict. The last block shows the golden assertions turn red on a
named break: the contract links dropped from the handler's rules.

Every chain assertion is shown red under a named mutation, recorded in NAMED_MUTATIONS below with the
chain it turns red (OMN-22901): a drill fault, a mutation of the events the handler publishes, or a
sample whose agreement falls under the frozen bar.
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
from omnimarket.nodes.node_code_graph_effect.handlers import code_graph_run
from omnimarket.nodes.node_code_graph_effect.handlers.code_graph_run import (
    CodeGraphRun,
    answer_command,
    load_topics,
)
from omnimarket.nodes.node_code_graph_effect.overlay import (
    ModelCodeGraphOverlay,
    ModelGraphSample,
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

#: The named mutations, each with the chain assertion it turns red and the test that shows it.
NAMED_MUTATIONS: dict[str, tuple[str, str]] = {
    "drop-contract-links": (
        "golden",
        "test_break_dropped_contract_links_turns_the_golden_chain_red",
    ),
    "unreadable-repository": (
        "golden",
        "test_break_unreadable_repository_turns_the_golden_chain_red",
    ),
    "drop-run-started-event": (
        "golden",
        "test_mutation_drop_run_started_event_turns_the_golden_chain_red",
    ),
    "failed-run-published-as-completed": (
        "error",
        "test_mutation_failed_run_published_as_completed_turns_the_error_chain_red",
    ),
    "failed-run-reports-working": (
        "error",
        "test_mutation_failed_run_reports_working_turns_the_error_chain_red",
    ),
    "two-labels-flipped-below-bar": (
        "golden",
        "test_error_chain_sample_below_bar_ends_not_working",
    ),
}
FLIPPED_UNUSED = ("src/example_core/unused.py", "src/example_app/orphan.py")


def _payload(fault: EnumGraphFault = EnumGraphFault.NONE) -> bytes:
    return json.dumps(
        {"correlation_id": CORRELATION, "write": True, "fault": fault.value}
    ).encode()


async def _publish(
    repos: ProtocolRepoSource,
    ledger: InMemoryLedger,
    payload: bytes,
    graph_overlay: ModelCodeGraphOverlay | None = None,
) -> list[tuple[str, dict[str, object]]]:
    handler = CodeGraphRun(
        overlay=graph_overlay or overlay(),
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


def _two_labels_flipped() -> ModelCodeGraphOverlay:
    """The example overlay with two unused labels flipped to used and the sample frozen again."""
    sample = overlay().sample
    entries = tuple(
        entry.model_copy(update={"expected": "used"})
        if entry.path in FLIPPED_UNUSED
        else entry
        for entry in sample.entries
    )
    frozen = ModelGraphSample.freeze(
        entries=entries, min_agree=sample.min_agree, controls=sample.controls
    )
    return overlay(sample=frozen.model_dump(mode="json"))


def test_error_chain_sample_below_bar_ends_not_working(
    repos: ProtocolRepoSource,
) -> None:
    """Mutation two-labels-flipped-below-bar: the run completes, reads NOT WORKING and names the bar."""
    ledger = InMemoryLedger()
    events = asyncio.run(
        _publish(repos, ledger, _payload(), graph_overlay=_two_labels_flipped())
    )
    assert [topic for topic, _ in events] == [
        TOPICS["code_graph_run_started"],
        TOPICS["code_graph_repo_read"],
        TOPICS["code_graph_repo_read"],
        TOPICS["code_graph_run_completed"],
    ]
    assert {body["correlation_id"] for _, body in events} == {CORRELATION}
    assert events[-1][1]["verdict"] == "NOT_WORKING"
    reasons = events[-1][1]["reasons"]
    assert isinstance(reasons, list)
    assert str(reasons[0]).startswith("below-bar:5-of-7")
    with pytest.raises(AssertionError):
        assert_golden_chain(events)
    row = evidence_row_from_text(ledger.lines[0])
    assert row is not None
    assert row.cells["verdict"] == "NOT_WORKING"
    assert row.cells["sample"] == "5/7"


def test_mutation_drop_run_started_event_turns_the_golden_chain_red(
    repos: ProtocolRepoSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation drop-run-started-event: the handler stops publishing its first event."""
    real = code_graph_run.events_of

    def mutated(result: Any, topics: dict[str, str]) -> Any:
        return [
            event
            for event in real(result, topics)
            if event[0] != topics["code_graph_run_started"]
        ]

    ledger = InMemoryLedger()
    assert_golden_chain(asyncio.run(_publish(repos, ledger, _payload())))
    monkeypatch.setattr(code_graph_run, "events_of", mutated)
    events = asyncio.run(_publish(repos, InMemoryLedger(), _payload()))
    with pytest.raises(AssertionError):
        assert_golden_chain(events)


def test_mutation_failed_run_published_as_completed_turns_the_error_chain_red(
    repos: ProtocolRepoSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation failed-run-published-as-completed: a failure is announced on the success topic."""
    real = code_graph_run.events_of
    fault = _payload(EnumGraphFault.UNREADABLE_REPO)
    assert_failed_chain(asyncio.run(_publish(repos, InMemoryLedger(), fault)))

    def mutated(result: Any, topics: dict[str, str]) -> Any:
        events = real(result, topics)
        topic, body = events[-1]
        if topic == topics["code_graph_run_failed"]:
            events[-1] = (topics["code_graph_run_completed"], body)
        return events

    monkeypatch.setattr(code_graph_run, "events_of", mutated)
    events = asyncio.run(_publish(repos, InMemoryLedger(), fault))
    with pytest.raises(AssertionError):
        assert_failed_chain(events)


def test_mutation_failed_run_reports_working_turns_the_error_chain_red(
    repos: ProtocolRepoSource, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mutation failed-run-reports-working: the failure event carries a WORKING verdict."""
    real = code_graph_run.events_of
    fault = _payload(EnumGraphFault.UNREADABLE_REPO)

    def mutated(result: Any, topics: dict[str, str]) -> Any:
        events = real(result, topics)
        topic, body = events[-1]
        events[-1] = (topic, {**body, "verdict": "WORKING"})
        return events

    monkeypatch.setattr(code_graph_run, "events_of", mutated)
    events = asyncio.run(_publish(repos, InMemoryLedger(), fault))
    with pytest.raises(AssertionError):
        assert_failed_chain(events)


def test_every_named_mutation_has_a_test_in_this_module() -> None:
    names = {
        name
        for name in globals()
        if name.startswith("test_") and callable(globals()[name])
    }
    assert {test for _, test in NAMED_MUTATIONS.values()} <= names
    assert {kind for kind, _ in NAMED_MUTATIONS.values()} == {"golden", "error"}


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
