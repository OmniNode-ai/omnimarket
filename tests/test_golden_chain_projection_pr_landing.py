# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19833 golden chain: the contract, the two projection traps, the writer.

Rule 7a names two ways to wire a projection wrong, and both have the same
symptom: every message consumed, offsets committed, the table at zero rows and
nothing raised. Each has a test here that fails when the trap is sprung.

  * Trap one: a pure entry on the projection arm. The fold returns rows and
    persists none, so a runtime that dispatches it alone writes nothing.
  * Trap two: a runner-shaped writer that does not declare in-process
    dispatch. The shared runtime classifies it as a standalone runner and
    never dispatches it, and this node has no standalone Deployment.

The trap-two test asks the runtime's own predicate, not a restatement of it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    _is_standalone_projection_runner,
)

from omnimarket.nodes.node_pr_landing_orchestrator.event_topics import (
    PR_LANDING_EVENT_TOPICS,
)
from omnimarket.nodes.node_projection_pr_landing.handlers import (
    HandlerProjectionPrLanding,
    PrLandingProjectionWriter,
)
from omnimarket.nodes.node_projection_pr_landing.handlers.handler_pr_landing_writer import (
    TOPIC_EVENT_KIND,
)
from tests.pr_landing_projection_events import (
    T0,
    S,
    a_reopened_pr_life,
    agent_needed,
    merged,
    transitioned,
)

pytestmark = pytest.mark.unit

_OUT_TOPIC = "onex.evt.omnimarket.projection-pr-landing-applied.v1"  # onex-topic-allow: asserted against the contract
_DLQ_TOPIC = "onex.dlq.omnimarket.projection-pr-landing-malformed.v1"  # onex-topic-allow: asserted against the contract

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_pr_landing"
    / "contract.yaml"
)


class _LoopBoundPool:
    """Reduced asyncpg: usable only from the loop that created it."""

    def __init__(self) -> None:
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def check(self) -> None:
        if self.closed:
            raise RuntimeError("pool is closed")
        if asyncio.get_running_loop() is not self.loop:
            raise RuntimeError("Event loop is closed")


class _RecordingAdapter:
    """Stands in for the asyncpg adapter and enforces its loop affinity."""

    def __init__(self, *, refuse_all: bool = False) -> None:
        self._pool: _LoopBoundPool | None = None
        self.refuse_all = refuse_all
        self.connects = 0
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def connect(self) -> None:
        self._pool = _LoopBoundPool()
        self.connects += 1

    async def close(self) -> None:
        if self._pool is not None:
            self._pool.closed = True
            self._pool = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self._pool is not None, "call connect() first"
        self._pool.check()
        self.calls.append((query, params))
        if "RETURNING" in query and not self.refuse_all:
            return [{"seq": params[2], "projection_cursor": len(self.calls)}]
        return []


def _load_contract() -> dict[str, Any]:
    with open(_CONTRACT) as handle:
        loaded = yaml.safe_load(handle)
    assert isinstance(loaded, dict)
    return loaded


def _writer(**kwargs: Any) -> PrLandingProjectionWriter:
    instance = PrLandingProjectionWriter()
    instance._db = _RecordingAdapter(**kwargs)  # type: ignore[assignment]
    return instance


def _adapter(writer: PrLandingProjectionWriter) -> _RecordingAdapter:
    adapter = writer.db
    assert isinstance(adapter, _RecordingAdapter)
    return adapter


# --------------------------------------------------------------------------
# The declared chain.
# --------------------------------------------------------------------------


def _publishers_of(topic: str) -> list[str]:
    """Every omnimarket node contract that declares it publishes ``topic``."""
    publishers: list[str] = []
    for path in sorted(_CONTRACT.parent.parent.glob("*/contract.yaml")):
        if topic not in path.read_text(encoding="utf-8"):
            continue
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        bus = raw.get("event_bus") or {}
        declared = list(bus.get("publish_topics") or [])
        declared += [e.get("topic") for e in raw.get("published_events") or []]
        if topic in declared:
            publishers.append(path.parent.name)
    return publishers


def test_the_consumer_declaration_lands_with_its_publisher() -> None:
    """All four subscriptions exactly when the orchestrator publishes all four.

    Before the orchestrator handler (OMN-19829) declares its publications,
    this node subscribes to nothing, because the HARD graph gate refuses a
    consumer with no producer. From the commit that adds the publisher, the
    four subscriptions must be present, or the events are published to nobody.
    Either half without the other fails here.
    """
    owned = set(PR_LANDING_EVENT_TOPICS.values())
    published = {topic for topic in owned if _publishers_of(topic)}
    subscribed = set(_load_contract()["event_bus"]["subscribe_topics"])
    assert published in (set(), owned), f"a partial publisher: {sorted(published)}"
    assert subscribed == published
    assert set(_writer().subscribe_topics) == subscribed


def test_the_writer_maps_every_topic_it_will_subscribe() -> None:
    assert set(TOPIC_EVENT_KIND) == set(PR_LANDING_EVENT_TOPICS.values())


def test_the_contract_declares_the_applied_and_dlq_topics() -> None:
    bus = _load_contract()["event_bus"]
    assert bus["publish_topics"] == [_OUT_TOPIC]
    assert bus["dlq_topics"] == [_DLQ_TOPIC]
    assert _load_contract()["terminal_event"] == _OUT_TOPIC
    assert _load_contract()["externally_consumed_topics"] == [_OUT_TOPIC]


def test_the_contract_declares_both_tables_read_write() -> None:
    """Without the table block the consumer advances offsets and never folds."""
    tables = {t["name"]: t for t in _load_contract()["db_io"]["db_tables"]}
    assert set(tables) == {"pr_landing_state", "pr_landing_transitions"}
    for table in tables.values():
        assert table["schema"] == "omninode_internal"
        assert table["access"] == "read_write"


def test_the_contract_declares_one_ordering_authority() -> None:
    db_io = _load_contract()["db_io"]
    assert db_io["ordering_key"] == "seq"
    assert db_io["dedupe_key"] == ["repository", "pr_number"]


def test_the_contract_routes_both_halves_of_the_pair() -> None:
    handlers = _load_contract()["handler_routing"]["handlers"]
    by_operation = {entry["operation"]: entry["handler"]["name"] for entry in handlers}
    assert by_operation == {
        "projection_pr_landing": "HandlerProjectionPrLanding",
        "pr_landing_projection_writer": "PrLandingProjectionWriter",
    }


# --------------------------------------------------------------------------
# AC2: the two traps.
# --------------------------------------------------------------------------


def test_trap_a_pure_entry_on_the_projection_arm_writes_no_rows() -> None:
    """The fold derives rows and persists none; only the writer writes.

    If the runtime dispatched the fold alone, the event would validate, the
    result would be published, the offset would commit and no table would
    change. So the fold must hold no database and must not claim the
    in-process capability, and the writer, given the same event, must write.
    """
    event = transitioned(9, S.CHECKS_PENDING, S.READY, "verdict_green", arm=True)
    fold = HandlerProjectionPrLanding()
    request = PrLandingProjectionWriter.build_request(str(event["_topic"]), event)
    result = fold.handle(request)

    assert result.transition_row is not None, "the fold does derive the rows"
    assert not hasattr(fold, "db"), "the fold owns no database, so it writes nothing"
    assert not getattr(fold, "onex_runtime_inprocess_dispatch", False)

    writer = _writer()
    written = writer.handle(dict(event))
    assert written["rows_written"] == 2
    statements = [query for query, _ in _adapter(writer).calls]
    assert any(
        "INSERT INTO omninode_internal.pr_landing_transitions" in q for q in statements
    )
    assert any(
        "INSERT INTO omninode_internal.pr_landing_state" in q for q in statements
    )


def test_trap_a_writer_without_in_process_dispatch_is_not_dispatched() -> None:
    """The runtime's own predicate: undeclared means standalone, dispatched by nobody."""
    assert PrLandingProjectionWriter.onex_runtime_inprocess_dispatch is True
    assert _is_standalone_projection_runner(PrLandingProjectionWriter()) is False

    class _Undeclared(PrLandingProjectionWriter):
        onex_runtime_inprocess_dispatch = False

    assert _is_standalone_projection_runner(_Undeclared()) is True, (
        "positive control: the same writer without the declaration is skipped"
    )


def test_the_in_process_capability_sits_on_the_writer_operation_only() -> None:
    """On the fold's operation it would dispatch the node twice."""
    for entry in _load_contract()["handler_routing"]["handlers"]:
        klass = (
            PrLandingProjectionWriter
            if entry["handler"]["name"] == "PrLandingProjectionWriter"
            else HandlerProjectionPrLanding
        )
        declared = bool(getattr(klass, "onex_runtime_inprocess_dispatch", False))
        assert declared is entry["operation"].endswith("_projection_writer")


def test_the_writer_calls_the_fold_rather_than_deriving_its_own_rows() -> None:
    assert isinstance(PrLandingProjectionWriter()._derive, HandlerProjectionPrLanding)


# --------------------------------------------------------------------------
# The writer, against a loop-affine recording double.
# --------------------------------------------------------------------------


def test_the_writer_entry_returns_a_row_count() -> None:
    result = _writer().handle(merged(11, 1))
    assert result["rows_written"] == 1
    assert result["event_kind"] == "merged"
    assert result["state_write_refused"] is False


def test_every_event_of_a_pr_life_writes_through_one_loop_each() -> None:
    """One event loop per message: nothing loop-bound survives across calls."""
    writer = _writer()
    life = a_reopened_pr_life()
    for event in life:
        assert writer.handle(dict(event))["rows_written"] >= 1
    assert _adapter(writer).connects == len(life)


def test_a_refused_write_is_not_counted() -> None:
    writer = _writer(refuse_all=True)
    result = writer.handle(transitioned(3, S.OBSERVED, S.PARKED, "evaluated_parked"))
    assert result["rows_written"] == 0
    assert result["state_write_refused"] is True
    assert len(_adapter(writer).calls) == 2, "both statements were attempted"


def test_only_a_transition_appends_to_the_log() -> None:
    writer = _writer()
    writer.handle(agent_needed(6))
    writer.handle(merged(11, 1))
    statements = [query for query, _ in _adapter(writer).calls]
    assert not any("pr_landing_transitions" in q for q in statements)


def test_the_upsert_carries_the_stale_write_guard_in_sql() -> None:
    writer = _writer()
    writer.handle(transitioned(1, None, S.OBSERVED, "pushed"))
    upsert = next(
        q
        for q, _ in _adapter(writer).calls
        if "ON CONFLICT (repository, pr_number)" in q
    )
    assert "WHERE omninode_internal.pr_landing_state.seq < EXCLUDED.seq" in upsert
    assert "RETURNING" in upsert


def test_the_log_is_appended_and_never_updated() -> None:
    writer = _writer()
    writer.handle(transitioned(1, None, S.OBSERVED, "pushed"))
    append = next(q for q, _ in _adapter(writer).calls if "pr_landing_transitions" in q)
    assert "ON CONFLICT (repository, pr_number, seq) DO NOTHING" in append
    assert "UPDATE" not in append


def test_the_writer_binds_the_event_time_not_a_string() -> None:
    """The double accepts any type; the real-Postgres module proves the column."""
    writer = _writer()
    writer.handle(transitioned(1, None, S.OBSERVED, "pushed", time=T0))
    _, params = next(
        (q, p) for q, p in _adapter(writer).calls if "pr_landing_transitions" in q
    )
    assert params[-1] == T0
