# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171 AC1: the golden chain of node_projection_eval_results on the in-memory bus.

Three code runs (a tracking run with a task tried three times, a second tracking run that closes
void, and a non-tracking run that stays in the tables), five judged events (one call judged twice)
and a trace for every code item go over all four source topics. The tables hold exactly the
expected item, trace and run-rate rows, and replaying every event, in the same order and in the
reverse order, gives the same tables.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.event_bus.model_event_headers import ModelEventHeaders
from omnibase_core.models.event_bus.model_event_message import ModelEventMessage

from omnimarket.handlers.stats_wilson import wilson_interval
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    SOURCE_TOPICS,
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_RUN_CLOSED,
    TOPIC_SNAPSHOT,
    TOPIC_TRACE,
)
from omnimarket.projection.runner import MessageMeta
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    TENANT,
    MemoryStore,
    at,
    close_event,
    code_event,
    judged_event,
    make_writer,
    trace_parts,
    try_row,
)

pytestmark = pytest.mark.unit
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_eval_results"
)
RUN_A = "00000000-0000-0000-0000-0000000000a1"  # tracking, closes closed: t1 tried three times
RUN_B = "00000000-0000-0000-0000-0000000000b2"  # tracking, closes void
RUN_C = "00000000-0000-0000-0000-0000000000c3"  # not a tracking run
CALLS = [f"00000000-0000-0000-0000-00000000d00{i}" for i in range(1, 5)]
Event = tuple[str, dict[str, Any], datetime]


def _fixture() -> list[Event]:
    events: list[Event] = []
    a_rows = [
        try_row(RUN_A, "t1", ordinal=1, verdict="fail_check", band="S"),
        try_row(RUN_A, "t1", ordinal=2, verdict="pass", band="S"),
        try_row(RUN_A, "t1", ordinal=3, verdict="pass", band="S"),
        try_row(RUN_A, "t2", verdict="pass", band="S"),
        try_row(
            RUN_A, "t3", verdict="unscorable", source="draft_not_recoverable", band="M"
        ),
    ]
    events.append((TOPIC_CODE_TRY, code_event(RUN_A, *a_rows), at(10)))
    events.append(
        (
            TOPIC_RUN_CLOSED,
            close_event(
                RUN_A,
                (("t1", MODEL, "S"), ("t2", MODEL, "S"), ("t3", MODEL, "M")),
                closed_at=at(12),
            ),
            at(12),
        )
    )
    b_rows = [try_row(RUN_B, "t1"), try_row(RUN_B, "t2", verdict="fail_check")]
    events.append((TOPIC_CODE_TRY, code_event(RUN_B, *b_rows), at(20)))
    events.append(
        (
            TOPIC_RUN_CLOSED,
            close_event(
                RUN_B,
                (("t1", MODEL, "S"), ("t2", MODEL, "S")),
                void_reason="infrastructure_unscorable_first_tries",
                closed_at=at(22),
            ),
            at(22),
        )
    )
    c_rows = [try_row(RUN_C, "t1"), try_row(RUN_C, "t2")]
    events.append((TOPIC_CODE_TRY, code_event(RUN_C, *c_rows), at(30)))
    events.append(
        (
            TOPIC_RUN_CLOSED,
            close_event(
                RUN_C,
                (("t1", MODEL, "S"), ("t2", MODEL, "S")),
                series="",
                closed_at=at(32),
            ),
            at(32),
        )
    )
    # Five judged events over one live week: CALLS[0] judged twice, the later under a new reviewer version.
    events.append(
        (TOPIC_JUDGED, judged_event(CALLS[0], accept=True, judged_at=at(40)), at(40))
    )
    events.append(
        (
            TOPIC_JUDGED,
            judged_event(
                CALLS[0], accept=False, judged_at=at(45), judge_version="judge-2"
            ),
            at(45),
        )
    )
    events.append(
        (TOPIC_JUDGED, judged_event(CALLS[1], accept=True, judged_at=at(41)), at(41))
    )
    events.append(
        (TOPIC_JUDGED, judged_event(CALLS[2], accept=True, judged_at=at(42)), at(42))
    )
    events.append(
        (
            TOPIC_JUDGED,
            judged_event(CALLS[3], model="other-model", accept=False, judged_at=at(43)),
            at(43),
        )
    )
    # A trace for every code item, one scoring each.
    for run, rows in ((RUN_A, a_rows), (RUN_B, b_rows), (RUN_C, c_rows)):
        for row in rows:
            for part in trace_parts(
                run,
                row["task_id"],
                ordinal=row["try_ordinal"],
                text=f"{run} {row['task_id']}",
            ):
                events.append((TOPIC_TRACE, part, at(50)))
    return events


async def _run_on_the_bus(
    events: list[Event], store: MemoryStore
) -> list[tuple[str, dict[str, Any]]]:
    """Publish every event on the in-memory bus; one subscriber per source topic feeds the writer."""
    writer, published = make_writer(store)
    bus = EventBusInmemory(environment="test", group="eval-results")
    await bus.start()

    async def on_message(message: ModelEventMessage) -> None:
        await writer._project(
            message.topic,
            {
                **json.loads(message.value),
                "_envelope_timestamp": message.headers.timestamp.isoformat(),
                "_tenant_id": TENANT,
            },
            MessageMeta(
                partition=0,
                offset=int(message.offset),
                fallback_id="",
                topic=message.topic,
            ),
        )

    for topic in SOURCE_TOPICS:
        await bus.subscribe(
            topic, on_message=on_message, group_id=f"eval-results-{topic}"
        )
    for topic, payload, when in events:
        await bus.publish(
            topic,
            None,
            json.dumps(payload).encode(),
            ModelEventHeaders(timestamp=when, source="test", event_type=topic),
        )
    await bus.close()
    return published


async def test_golden_chain_fills_the_three_tables_and_replay_changes_nothing() -> None:
    events = _fixture()
    store = MemoryStore()
    published = await _run_on_the_bus(events, store)

    # Items: A 5 tries, B 2, C 2, and four distinct judged calls; none empty.
    assert len(store.items) == 5 + 2 + 2 + 4
    assert len(store.parts) == 5 + 2 + 2, "one single-part trace per code item"
    assert store.rates
    assert store.runs
    # The standing row of the re-judged call is the later judgement.
    (rejudged,) = [i for i in store.items.values() if i.task_id == CALLS[0]]
    assert rejudged.passed is False
    assert rejudged.scorer_version == "v1+judge-2"

    rates = {(r.run_id, r.model_id, r.size_band): r for r in store.rates}
    # Run A: t1 counts once at its first try (a fail), t2 passes, t3 is a model-class unscorable try.
    assert [
        (rates[(RUN_A, MODEL, band)].n, rates[(RUN_A, MODEL, band)].passed)
        for band in ("S", "M", "all")
    ] == [
        (2, 1),
        (1, 0),
        (3, 1),
    ]
    row = rates[(RUN_A, MODEL, "all")]
    assert row.run_state.value == "closed"
    assert row.unscorable_model == 1
    assert (row.wilson_low, row.wilson_high) == wilson_interval(1, 3)
    assert rates[(RUN_B, MODEL, "all")].run_state.value == "void"
    assert (
        rates[(RUN_B, MODEL, "all")].void_reason
        == "infrastructure_unscorable_first_tries"
    )
    assert rates[(RUN_C, MODEL, "all")].run_state.value == "closed"
    live = [r for r in store.rates if r.run_state.value == "live"]
    assert {(r.model_id, r.n, r.passed) for r in live} == {
        (MODEL, 3, 2),
        ("other-model", 1, 0),
    }
    assert next(r for r in live if r.model_id == MODEL).scorer_versions == (
        "v1+judge-1",
        "v1+judge-2",
    )

    # The snapshot: only the tracking runs, the closed one with rates and the void one with its cause.
    topic, snapshot = published[-1]
    assert topic == TOPIC_SNAPSHOT
    (shown,) = snapshot["models"]
    assert {r["run_id"]: r["state"] for r in shown["runs"]} == {
        RUN_A: "closed",
        RUN_B: "void",
    }
    assert len(shown["live_weeks"]) == 1
    assert shown["live_weeks"][0]["n"] == 3

    # Replay: the same events again, then in the reverse order, leave the tables unchanged.
    before = store.tables()
    await _run_on_the_bus(events, store)
    assert store.tables() == before
    await _run_on_the_bus(list(reversed(events)), store)
    assert store.tables() == before


async def test_the_golden_chain_in_another_order_reaches_the_same_tables() -> None:
    events = _fixture()
    forward, backward = MemoryStore(), MemoryStore()
    await _run_on_the_bus(events, forward)
    await _run_on_the_bus(list(reversed(events)), backward)
    assert forward.tables() == backward.tables()


def test_the_contract_subscribes_to_the_four_source_topics_and_publishes_the_snapshot() -> (
    None
):
    contract = yaml.safe_load((_NODE / "contract.yaml").read_text())
    subscribed = set(contract["event_bus"]["subscribe_topics"])
    assert set(SOURCE_TOPICS) <= subscribed
    assert contract["event_bus"]["publish_topics"] == [TOPIC_SNAPSHOT]
    assert {t["name"] for t in contract["db_io"]["db_tables"]} == {
        "eval_item_scores",
        "eval_item_traces",
        "eval_run_closes",
        "eval_run_rates",
    }
