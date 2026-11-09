# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain for the goal-trace writer (OMN-20034, GC.11).

The writer is exercised through its real ``_project`` against an in-memory
stand-in for the two tables that applies the same statements the writer sends
(one handler per SQL constant of the writer module). The stand-in implements the
key, the conflict clause and the scope read; the real-Postgres read of AC3 is a
separate lab step.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml

from omnimarket.enums.enum_dod_verify_status import EnumDodVerifyStatus
from omnimarket.nodes.node_projection_dod_verdict.models import ModelDodVerdictWire
from omnimarket.nodes.node_projection_goal_trace.handlers import (
    handler_goal_trace_writer as writer_module,
)
from omnimarket.nodes.node_projection_goal_trace.handlers.handler_goal_trace_writer import (
    GoalTraceProjectionWriter,
)

pytestmark = pytest.mark.unit

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_goal_trace"
)
GOAL = UUID("11111111-1111-4111-8111-111111111111")
OTHER_GOAL = UUID("22222222-2222-4222-8222-222222222222")
T0 = datetime(2026, 11, 9, 10, 0, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    with open(_NODE_DIR / "contract.yaml") as handle:
        loaded: dict[str, Any] = yaml.safe_load(handle)
    return loaded


class _FakeDb:
    """In-memory goal_trace_events, goal_traces and claude_agent_spans."""

    def __init__(self) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self.traces: dict[UUID, tuple[Any, ...]] = {}
        self.spans: dict[tuple[str, str], dict[str, Any]] = {}
        self.statements: list[str] = []

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        self.statements.append(" ".join(query.split())[:48])
        if query is writer_module._SELECT_PARENT_SPAN:
            span = self.spans.get((params[0], params[1]))
            return [span] if span else []
        if query is writer_module._INSERT_EVENT:
            return self._insert(params)
        if query is writer_module._SELECT_SCOPE:
            return self._scope(params[0], set(params[1]))
        if query is writer_module._UPDATE_ROW:
            record = self.events[(params[0], params[1])]
            record.update(
                goal_id=params[2],
                seq=params[3],
                assignment=params[4],
                id_conflict=params[5],
            )
            return [{"event_id": params[0]}]
        if query is writer_module._UPSERT_TRACE:
            self.traces[params[0]] = params
            return [{"goal_id": params[0]}]
        raise AssertionError(f"unexpected statement: {query[:60]}")

    def _insert(self, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        key = (params[0], params[1])
        parent = params[7]
        if key in self.events:
            stored = self.events[key]
            if stored["parent_agent_key"] is None and parent is not None:
                stored["parent_agent_key"] = parent
                return [{"event_id": params[0], "event_inserted": False}]
            return []
        names = [
            "event_id",
            "body_digest",
            "kind",
            "raw_goal_id",
            "parent_goal_id",
            "session_id",
            "agent_key",
            "parent_agent_key",
            "emitted_at",
            "revision",
            "replaces",
            "completed_at",
            "verdict",
            "item_hash",
        ]
        self.events[key] = {
            **dict(zip(names, params, strict=True)),
            "goal_id": None,
            "seq": None,
            "assignment": None,
            "id_conflict": False,
        }
        return [{"event_id": params[0], "event_inserted": True}]

    def _scope(self, session: str | None, goals: set[UUID]) -> list[dict[str, Any]]:
        rows = []
        for record in self.events.values():
            if (
                (session is not None and record["session_id"] == session)
                or record["goal_id"] in goals
                or record["raw_goal_id"] in goals
            ):
                rows.append(
                    {
                        **record,
                        "stored_goal_id": record["goal_id"],
                        "stored_seq": record["seq"],
                        "stored_assignment": record["assignment"],
                        "stored_id_conflict": record["id_conflict"],
                    }
                )
        return rows


def _writer() -> GoalTraceProjectionWriter:
    return GoalTraceProjectionWriter(pool_settings=None)


_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "claude_hook_capture"


def _fixture(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (_FIXTURES / "events" / name).read_text("utf-8")
    )
    return loaded


def _hook(
    agent: str | None,
    minute: int,
    *,
    goal: UUID | None = None,
    session: str = "s1",
) -> dict[str, Any]:
    """A captured hook event derived from the producer's own fixture."""
    event = _fixture("PreToolUse.goal_binding.json" if goal else "PreToolUse.json")
    event["event_id"] = str(uuid4())
    event["emitted_at"] = (T0 + timedelta(minutes=minute)).isoformat()
    event["lineage"]["session_id"] = session
    event["lineage"]["agent_id"] = agent
    event["lineage"]["is_subagent"] = agent is not None
    event["lineage"]["tool_use_id"] = f"t{minute}"
    if goal:
        event["payload"]["goal_id"] = str(goal)
        event["payload"].pop("parent_goal_id")
    return event


def _trace_event(
    event_id: str, kind: str, minute: int, **fields: Any
) -> dict[str, Any]:
    return {
        "event_id": event_id,
        "kind": kind,
        "body_digest": f"d-{event_id}",
        "emitted_at": (T0 + timedelta(minutes=minute)).isoformat(),
        **fields,
    }


def _verdict(
    goal: UUID, revision: UUID, minute: int, *, failed: int = 0
) -> dict[str, Any]:
    """A completed verification, built through the producer's own wire model."""
    wire = ModelDodVerdictWire(
        correlation_id=uuid4(),
        ticket_id="OMN-20034",
        status=EnumDodVerifyStatus.VERIFIED,
        started_at=T0 + timedelta(minutes=minute - 1),
        completed_at=T0 + timedelta(minutes=minute),
        total_checks=3,
        verified_count=3 - failed,
        failed_count=failed,
        behavior_proving_count=2,
        goal_id=goal,
        contract_revision=revision,
    )
    return wire.model_dump(mode="json")


def _replay(db: _FakeDb, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    writer = _writer()

    async def run() -> list[dict[str, Any]]:
        return [await writer._project(db, "topic", m) for m in messages]

    return asyncio.run(run())


def _messages() -> list[dict[str, Any]]:
    revision = UUID("33333333-3333-4333-8333-333333333333")
    return [
        _trace_event(str(GOAL), "claim", 0, goal_id=str(GOAL)),
        _trace_event(str(revision), "revise", 1, goal_id=str(GOAL), replaces=str(GOAL)),
        _hook("lane", 2, goal=GOAL),
        _hook("lane", 3),
        _hook("lane", 4),
        _hook("lane", 5),
        _hook("stray", 6),
        _trace_event("close-1", "close", 7, goal_id=str(GOAL), revision=str(revision)),
        _verdict(GOAL, revision, 8, failed=1),
        _verdict(GOAL, revision, 9),
    ]


def _state(db: _FakeDb) -> dict[str, Any]:
    return {
        "events": {
            k: (v["goal_id"], v["seq"], v["assignment"], v["id_conflict"])
            for k, v in db.events.items()
        },
        "traces": {g: (p[9], p[14]) for g, p in db.traces.items()},
    }


# ---------------------------------------------------------------------- AC3


def test_the_writer_writes_rows_for_a_replay_and_a_second_replay_changes_nothing() -> (
    None
):
    """AC3 shape: the replay wrote its own rows for the goal; a redelivery adds none."""
    db = _FakeDb()
    messages = _messages()
    before = sum(1 for r in db.events.values() if r["goal_id"] == GOAL)
    first = _replay(db, messages)
    after = sum(1 for r in db.events.values() if r["goal_id"] == GOAL)
    assert before == 0
    assert after > before  # the falsifier: zero, or equal to before
    assert all(result["rows_upserted"] > 0 for result in first)
    assert db.traces[GOAL][9] == "done"  # the latest attempt of the closed revision

    state = _state(db)
    count = len(db.events)
    second = _replay(db, messages)
    assert len(db.events) == count
    assert all(result["event_inserted"] is False for result in second)
    assert _state(db) == state


def test_the_stray_agent_is_stored_unassigned_and_never_attached() -> None:
    db = _FakeDb()
    _replay(db, _messages())
    stray = [r for r in db.events.values() if r["agent_key"] == "s1:stray"]
    assert len(stray) == 1
    assert stray[0]["goal_id"] is None
    assert stray[0]["assignment"] == "unassigned"


def test_the_final_state_is_the_same_for_every_delivery_order() -> None:
    reference_db = _FakeDb()
    messages = _messages()
    _replay(reference_db, messages)
    reference = _state(reference_db)
    rng = random.Random(11)
    for _ in range(12):
        shuffled = list(messages) + [rng.choice(messages) for _ in range(3)]
        rng.shuffle(shuffled)
        db = _FakeDb()
        _replay(db, shuffled)
        assert _state(db) == reference


def test_a_binding_that_arrives_last_re_attaches_the_earlier_hooks() -> None:
    db = _FakeDb()
    early = [_hook("lane", i) for i in range(1, 4)]
    _replay(db, early)
    assert all(r["goal_id"] is None for r in db.events.values())
    results = _replay(db, [_hook("lane", 9, goal=GOAL)])
    assert all(r["goal_id"] == GOAL for r in db.events.values())
    assert (
        results[0]["rewritten_rows"] == 4
    )  # three re-attached, plus the binding row itself


def test_a_child_agent_joins_through_the_parent_the_span_table_names() -> None:
    db = _FakeDb()
    db.spans[("s1", "child")] = {
        "parent_agent_id": "lane",
        "parent_resolution": "resolved",
    }
    _replay(db, [_hook("lane", 1, goal=GOAL), _hook("child", 2)])
    child = next(r for r in db.events.values() if r["agent_key"] == "s1:child")
    assert child["goal_id"] == GOAL
    assert child["assignment"] == "ancestry"


# ---------------------------------------------------------------- error chain


def test_a_payload_that_is_no_source_is_refused_before_any_write() -> None:
    db = _FakeDb()
    with pytest.raises(ValueError, match=r"[a-z]"):
        _replay(db, [{"hello": "world"}])
    assert not db.events
    assert not db.statements


def test_a_content_bearing_trace_event_is_refused_before_any_write() -> None:
    db = _FakeDb()
    bad = _trace_event("x", "hook", 0, prompt="do the thing")
    with pytest.raises(ValueError, match=r"[a-z]"):
        _replay(db, [bad])
    assert not db.events


def test_a_verification_that_is_not_goal_scoped_writes_nothing_and_says_so() -> None:
    db = _FakeDb()
    message = _verdict(GOAL, uuid4(), 5)
    message.pop("goal_id")
    (result,) = _replay(db, [message])
    assert result["rows_upserted"] == 0
    assert result["skipped"] == "not goal-scoped"
    assert not db.events


def test_a_rehearsal_is_not_an_attempt() -> None:
    db = _FakeDb()
    message = _verdict(GOAL, uuid4(), 5)
    message["dry_run"] = True
    (result,) = _replay(db, [message])
    assert result["rows_upserted"] == 0
    assert not db.events


def test_the_same_id_with_another_body_is_kept_as_a_conflict() -> None:
    db = _FakeDb()
    one = _trace_event(str(GOAL), "claim", 0, goal_id=str(GOAL))
    two = {**one, "body_digest": "other-body"}
    _replay(db, [one, two])
    assert len(db.events) == 2
    assert all(r["id_conflict"] for r in db.events.values())
    assert db.traces[GOAL][13] == [str(GOAL)]


def test_two_goals_in_one_session_stay_apart() -> None:
    db = _FakeDb()
    _replay(
        db,
        [
            _hook("a", 1, goal=GOAL),
            _hook("b", 2, goal=OTHER_GOAL),
            _hook("a", 3),
            _hook("b", 4),
        ],
    )
    for record in db.events.values():
        expected = GOAL if record["agent_key"] == "s1:a" else OTHER_GOAL
        assert record["goal_id"] == expected


def test_every_delivery_order_of_a_small_chain_agrees() -> None:
    messages = [
        _hook("lane", 1, goal=GOAL),
        _hook("lane", 2),
        _trace_event(str(GOAL), "claim", 0, goal_id=str(GOAL)),
    ]
    states = []
    for ordering in itertools.permutations(messages):
        db = _FakeDb()
        _replay(db, list(ordering))
        states.append(_state(db)["traces"])
    assert all(s == states[0] for s in states)


# -------------------------------------------------------------- contract facts


def test_the_writer_declares_in_process_dispatch() -> None:
    """Undeclared, the shared runtime skips it and every message writes zero rows."""
    assert GoalTraceProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_the_routing_dispatches_the_writer_alone() -> None:
    entries = _contract()["handler_routing"]["handlers"]
    assert [entry["handler"]["name"] for entry in entries] == [
        "GoalTraceProjectionWriter"
    ]
    assert _contract()["handler"]["class"] == "HandlerProjectionGoalTrace"


def test_the_writer_reads_its_topics_from_the_contract() -> None:
    writer = _writer()
    contract = _contract()
    assert writer.subscribe_topics == contract["event_bus"]["subscribe_topics"]
    assert len(writer.subscribe_topics) == 3
    assert writer.poison_dlq_topics == contract["event_bus"]["dlq_topics"]


def test_the_migration_creates_the_tables_the_contract_declares() -> None:
    sql = (_NODE_DIR / "migrations" / "0000_create_goal_trace_events.sql").read_text()
    for table in _contract()["db_io"]["db_tables"]:
        if table["access"] == "read":
            continue
        assert f"CREATE TABLE IF NOT EXISTS omninode_internal.{table['name']}" in sql
    assert "PRIMARY KEY (event_id, body_digest)" in sql


def test_the_adapter_accepts_what_the_capture_wire_accepts_and_refuses_what_it_refuses() -> (
    None
):
    """Producer and consumer pin each other, in both directions, fixture by fixture."""
    from omnimarket.nodes.node_projection_claude_hook_events.models import (
        ModelClaudeHookEventWire,
    )
    from omnimarket.nodes.node_projection_goal_trace.handlers.handler_goal_trace_adapters import (
        to_trace_event,
    )

    accepted = refused = 0
    for path in sorted((_FIXTURES / "events").glob("*.json")):
        event = json.loads(path.read_text("utf-8"))
        if "hook_event_name" not in event:
            continue
        try:
            ModelClaudeHookEventWire.model_validate(event)
        except ValueError:
            with pytest.raises(ValueError, match=r"[a-z]"):
                to_trace_event(event)
            refused += 1
            continue
        traced = to_trace_event(event)
        assert traced is not None, path.name
        assert traced.event_id == event["event_id"]
        accepted += 1
    assert accepted > 10
    assert refused >= 1


def test_the_binding_fixture_binds_its_agent_to_the_stamped_goal() -> None:
    from omnimarket.nodes.node_projection_goal_trace.handlers.handler_goal_trace_adapters import (
        to_trace_event,
    )

    traced = to_trace_event(_fixture("PreToolUse.goal_binding.json"))
    assert traced is not None
    assert str(traced.goal_id) == "0b8f5c1e-5d0c-4f43-9a52-3c7f1d2e9a10"
    assert str(traced.parent_goal_id) == "7a1d9e44-2b6f-4c8e-8f01-b5d3a9c60e27"
    plain = to_trace_event(_fixture("PreToolUse.json"))
    assert plain is not None
    assert plain.goal_id is None
