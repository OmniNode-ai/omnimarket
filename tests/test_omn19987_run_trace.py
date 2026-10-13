# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19987: the run-trace projection keeps one run's steps in the orchestrator's order.

The delegate orchestrator numbers each step of a run from one counter and
carries that number on the step event. The fold keys a row on (tenant,
correlation id, step sequence), so a run's order is that number and nothing
else. Run through the REAL SQLite adapter, so the store-neutral write path is
the thing under test, not a double.

Failure modes each test is written against:

* the order follows arrival: a chain delivered shuffled stores or reads back in
  delivery order (AC1);
* the order follows the clock: two steps with one timestamp swap, or tie (AC1);
* another tenant's step with the same correlation id lands in this tenant's run,
  or overwrites one of its rows (AC2);
* a replayed chain doubles the rows, or moves a row's stored time;
* a step with no tenant, no correlation id or no step sequence is keyed by a
  default (an empty tenant, the house tenant, sequence 0) instead of refused;
* a step that arrives after the terminal is dropped, or is filed after it;
* a redelivered step with different content silently rewrites the stored fact;
* a timed-out run marks the terminal as the failing step instead of the
  provider call that timed out (WF-2);
* an escalated run collapses its provider calls into one row;
* the exposure is not tenant-scoped, cannot be filtered to one run, or declares
  an order other than the step sequence.
"""

from __future__ import annotations

import itertools
import json
import random
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.enums.enum_run_step_kind import EnumRunStepKind
from omnimarket.enums.enum_run_step_outcome import EnumRunStepOutcome
from omnimarket.models.model_delegation_run_step import ModelDelegationRunStep
from omnimarket.nodes.node_projection_run_trace.handlers.handler_projection_run_trace import (
    HandlerProjectionRunTrace,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_store import (
    RUN_TRACE_TABLE,
    apply_run_step,
)
from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    RunTraceProjectionWriter,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.read_page import topic_supports_correlation_id_filter
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_run_trace"
)
CONTRACT_PATH = NODE_DIR / "contract.yaml"
FIXTURES = Path(__file__).parent / "fixtures" / "omn19987"
# Recorded: the step events one real delegation emitted on a lab host on
# 2026-10-11 (run eb99e731, answered by the host's local model on the first
# call), exactly as the orchestrator published them. The acceptance tests fold
# this chain.
RECORDED_PATH = FIXTURES / "recorded_run_step_chain.json"
# Constructed: an escalated run (a failed gate, a second routing decision, a
# second provider call), written by hand in the recorded chain's shape. It
# covers what one first-call success cannot.
CHAIN_PATH = FIXTURES / "run_step_chain.json"

OTHER_TENANT = "11111111-2222-4333-8444-555555555555"


def _chain() -> list[dict[str, Any]]:
    """The constructed escalated run, in step order."""
    chain: list[dict[str, Any]] = json.loads(CHAIN_PATH.read_text(encoding="utf-8"))
    return chain


def _recorded() -> list[dict[str, Any]]:
    """The recorded real run, in the order the orchestrator emitted it."""
    chain: list[dict[str, Any]] = json.loads(RECORDED_PATH.read_text(encoding="utf-8"))
    return chain


_CHAINS = {"recorded": _recorded, "constructed": _chain}


def _apply(db: SqliteDatabaseAdapter, events: list[dict[str, Any]]) -> list[bool]:
    fold = HandlerProjectionRunTrace()
    return [
        apply_run_step(fold.handle(ModelDelegationRunStep.model_validate(event)), db)
        for event in events
    ]


def _run_rows(db: SqliteDatabaseAdapter, tenant: str) -> list[dict[str, object]]:
    """The tenant's stored rows, read with the order the exposure declares."""
    return db.query(RUN_TRACE_TABLE, {"tenant_id": tenant}, order_by="step_sequence")


def _raw_rows(db: SqliteDatabaseAdapter) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(db.db_path)
    try:
        return conn.execute(
            f"SELECT * FROM {RUN_TRACE_TABLE} ORDER BY tenant_id, step_sequence"
        ).fetchall()
    finally:
        conn.close()


def test_the_recorded_chain_is_what_one_real_run_emitted() -> None:
    chain = _recorded()
    assert [event["step_kind"] for event in chain] == [
        "request",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    ]
    for event in chain:
        ModelDelegationRunStep.model_validate(event)
    call = chain[2]
    # A real provider answered: a measured, non-zero duration and a served model.
    assert call["duration_ms"] > 0
    assert call["model_id"]
    assert len(chain[1]["candidates"]) >= 2


@pytest.mark.parametrize("which", sorted(_CHAINS))
def test_the_chain_is_one_run_numbered_without_gaps(which: str) -> None:
    chain = _CHAINS[which]()
    assert len({event["correlation_id"] for event in chain}) == 1
    assert len({event["tenant_id"] for event in chain}) == 1
    assert [event["step_sequence"] for event in chain] == list(range(1, len(chain) + 1))
    assert chain[0]["step_kind"] == EnumRunStepKind.REQUEST.value
    assert chain[-1]["step_kind"] == EnumRunStepKind.TERMINAL.value


@pytest.mark.parametrize("which", sorted(_CHAINS))
@pytest.mark.parametrize("seed", [0, 1, 2, 3, 4, 5, 6, 7])
def test_a_shuffled_chain_with_two_equal_timestamps_stores_in_step_order(
    tmp_path: Path, seed: int, which: str
) -> None:
    chain = _CHAINS[which]()
    tenant = chain[0]["tenant_id"]
    # Two neighbouring steps share one timestamp, and a later step is stamped
    # before an earlier one: neither the clock nor arrival can give the order.
    chain[2]["started_at"] = chain[1]["started_at"]
    chain[-1]["started_at"] = chain[0]["started_at"]
    delivered = list(chain)
    random.Random(seed).shuffle(delivered)
    assert delivered != chain, "the delivery must differ from the step order"

    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    assert all(_apply(db, delivered))

    rows = _run_rows(db, tenant)
    assert [row["step_sequence"] for row in rows] == [
        event["step_sequence"] for event in chain
    ]
    assert [row["step_kind"] for row in rows] == [event["step_kind"] for event in chain]


@pytest.mark.parametrize("which", sorted(_CHAINS))
def test_reversed_delivery_stores_in_step_order(tmp_path: Path, which: str) -> None:
    chain = _CHAINS[which]()
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    _apply(db, list(reversed(chain)))
    rows = _run_rows(db, chain[0]["tenant_id"])
    assert [row["step_kind"] for row in rows] == [event["step_kind"] for event in chain]


@pytest.mark.parametrize("which", sorted(_CHAINS))
def test_another_tenants_step_never_joins_or_overwrites_the_run(
    tmp_path: Path, which: str
) -> None:
    chain = _CHAINS[which]()
    tenant = chain[0]["tenant_id"]
    # Same correlation id, same step sequence, different tenant: the closest a
    # foreign event can get to this run.
    foreign = {
        **chain[2],
        "tenant_id": OTHER_TENANT,
        "backend_id": "foreign-backend",
        "reason": "foreign",
    }
    delivered = [*chain[:3], foreign, *chain[3:]]
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    assert all(_apply(db, delivered))

    mine = _run_rows(db, tenant)
    assert len(mine) == len(chain)
    assert {row["tenant_id"] for row in mine} == {tenant}
    assert "foreign-backend" not in {row["backend_id"] for row in mine}
    assert mine[2]["backend_id"] == chain[2].get("backend_id")
    theirs = _run_rows(db, OTHER_TENANT)
    assert [(row["tenant_id"], row["backend_id"]) for row in theirs] == [
        (OTHER_TENANT, "foreign-backend")
    ]


@pytest.mark.parametrize("which", sorted(_CHAINS))
def test_replaying_the_chain_changes_nothing(tmp_path: Path, which: str) -> None:
    chain = _CHAINS[which]()
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    assert all(_apply(db, chain))
    before = _raw_rows(db)
    replayed = _apply(db, list(reversed(chain)))
    assert not any(replayed), "a replayed step must report that it wrote nothing"
    assert _raw_rows(db) == before
    assert len(before) == len(chain)


def test_a_redelivered_step_with_different_content_does_not_rewrite_the_fact(
    tmp_path: Path,
) -> None:
    chain = _chain()
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    _apply(db, chain)
    before = _raw_rows(db)
    altered = {**chain[1], "outcome": "failed", "reason": "rewritten"}
    assert _apply(db, [altered]) == [False]
    assert _raw_rows(db) == before


@pytest.mark.parametrize("which", sorted(_CHAINS))
def test_a_step_arriving_after_the_terminal_takes_its_own_place(
    tmp_path: Path, which: str
) -> None:
    chain = _CHAINS[which]()
    late = chain[3]
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    _apply(db, [event for event in chain if event is not late])
    assert _apply(db, [late]) == [True]
    rows = _run_rows(db, chain[0]["tenant_id"])
    assert [row["step_sequence"] for row in rows] == [
        event["step_sequence"] for event in chain
    ]


@pytest.mark.parametrize(
    "broken",
    [
        {"tenant_id": None},
        {"tenant_id": ""},
        {"tenant_id": "   "},
        {"correlation_id": None},
        {"correlation_id": ""},
        {"correlation_id": "not-a-uuid"},
        {"step_sequence": None},
        {"step_sequence": 0},
        {"step_sequence": -1},
        {"step_sequence": "first"},
        {"step_kind": "thinking"},
        {"outcome": "maybe"},
        {"started_at": None},
        {"started_at": "2026-10-11T10:00:00"},
        {"duration_ms": -1},
    ],
)
def test_a_step_missing_part_of_its_key_is_refused_not_given_a_default(
    tmp_path: Path, broken: dict[str, object]
) -> None:
    event = {**_chain()[1], **broken}
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    with pytest.raises(ValidationError):
        _apply(db, [event])
    assert db.query(RUN_TRACE_TABLE) == []


@pytest.mark.parametrize("absent", ["tenant_id", "correlation_id", "step_sequence"])
def test_a_step_with_a_key_field_absent_is_refused(tmp_path: Path, absent: str) -> None:
    event = dict(_chain()[1])
    del event[absent]
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    with pytest.raises(ValidationError):
        _apply(db, [event])
    assert db.query(RUN_TRACE_TABLE) == []


def test_a_timed_out_run_marks_the_provider_call_not_the_terminal(
    tmp_path: Path,
) -> None:
    chain = _chain()[:3]
    tenant = chain[0]["tenant_id"]
    timed_out_call = {
        **chain[2],
        "step_kind": "provider_call",
        "outcome": "failed",
        "failure_cause": "timeout",
    }
    terminal = {
        **chain[0],
        "step_sequence": 4,
        "step_kind": "terminal",
        "outcome": "failed",
        "failure_cause": "timeout",
        "failing_step_sequence": timed_out_call["step_sequence"],
    }
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    _apply(db, [terminal, timed_out_call, chain[1], chain[0]])
    rows = _run_rows(db, tenant)
    last = rows[-1]
    assert last["step_kind"] == "terminal"
    assert last["failing_step_sequence"] == timed_out_call["step_sequence"]
    assert last["failing_step_sequence"] != last["step_sequence"]
    marked = [
        row for row in rows if row["step_sequence"] == last["failing_step_sequence"]
    ]
    assert [(row["step_kind"], row["failure_cause"]) for row in marked] == [
        ("provider_call", "timeout")
    ]
    assert [row["failing_step_sequence"] for row in rows[:-1]] == [None] * 3


def test_a_terminal_cannot_name_itself_or_a_later_step_as_the_failing_step() -> None:
    base = {**_chain()[-1], "outcome": "failed", "failure_cause": "timeout"}
    sequence = base["step_sequence"]
    for wrong in (sequence, sequence + 1):
        with pytest.raises(ValidationError):
            ModelDelegationRunStep.model_validate(
                {**base, "failing_step_sequence": wrong}
            )


def test_only_a_failed_terminal_names_a_failing_step() -> None:
    chain = _chain()
    with pytest.raises(ValidationError):
        ModelDelegationRunStep.model_validate({**chain[1], "failing_step_sequence": 1})
    with pytest.raises(ValidationError):
        ModelDelegationRunStep.model_validate(
            {**chain[-1], "outcome": "succeeded", "failing_step_sequence": 1}
        )


def test_every_provider_call_of_an_escalated_run_keeps_its_own_row(
    tmp_path: Path,
) -> None:
    chain = _chain()
    calls = [e for e in chain if e["step_kind"] == EnumRunStepKind.PROVIDER_CALL.value]
    assert len(calls) >= 2, "the chain must be an escalated run"
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    _apply(db, chain)
    stored = [
        row
        for row in _run_rows(db, chain[0]["tenant_id"])
        if row["step_kind"] == EnumRunStepKind.PROVIDER_CALL.value
    ]
    assert [row["attempt_number"] for row in stored] == [
        call["attempt_number"] for call in calls
    ]
    assert [row["backend_id"] for row in stored] == [
        call["backend_id"] for call in calls
    ]


def test_the_routing_step_keeps_its_candidates_in_the_order_given(
    tmp_path: Path,
) -> None:
    chain = _chain()
    routing = next(
        e for e in chain if e["step_kind"] == EnumRunStepKind.ROUTING_DECISION.value
    )
    assert len(routing["candidates"]) >= 2
    db = SqliteDatabaseAdapter(tmp_path / "trace.sqlite")
    _apply(db, chain)
    row = next(
        r
        for r in _run_rows(db, chain[0]["tenant_id"])
        if r["step_sequence"] == routing["step_sequence"]
    )
    assert json.loads(str(row["candidates"])) == routing["candidates"]
    assert row["backend_id"] == routing["backend_id"]
    assert row["reason"] == routing["reason"]


def test_fold_is_pure_and_replay_deterministic() -> None:
    fold = HandlerProjectionRunTrace()
    for event in _chain():
        step = ModelDelegationRunStep.model_validate(event)
        assert fold.handle(step) == fold.handle(step)


def test_the_stored_time_is_utc_whatever_the_senders_offset() -> None:
    event = {**_chain()[0], "started_at": "2026-10-11T05:30:00+05:30"}
    row = HandlerProjectionRunTrace().handle(
        ModelDelegationRunStep.model_validate(event)
    )
    assert row.started_at.isoformat() == "2026-10-11T00:00:00+00:00"


def test_every_step_kind_and_outcome_the_event_may_carry_is_closed() -> None:
    assert {kind.value for kind in EnumRunStepKind} == {
        "request",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    }
    assert {outcome.value for outcome in EnumRunStepOutcome} == {"succeeded", "failed"}


def test_all_orderings_of_a_short_run_store_identically(tmp_path: Path) -> None:
    chain = _chain()[:4]
    expected: list[tuple[Any, ...]] | None = None
    for index, ordering in enumerate(itertools.permutations(chain)):
        db = SqliteDatabaseAdapter(tmp_path / f"trace-{index}.sqlite")
        _apply(db, list(ordering))
        stored = [
            (row["step_sequence"], row["step_kind"], row["started_at"], row["outcome"])
            for row in _run_rows(db, chain[0]["tenant_id"])
        ]
        if expected is None:
            expected = stored
        assert stored == expected


def _exposure() -> Any:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    exposures = load_projection_exposures_from_contract(
        contract, str(contract["name"]), CONTRACT_PATH
    )
    assert len(exposures) == 1
    return exposures[0]


def test_exposure_is_the_run_trace_topic_at_the_step_grain() -> None:
    exposure = _exposure()
    assert exposure.topic == "onex.snapshot.projection.run-trace.v1"
    assert exposure.table == RUN_TRACE_TABLE
    assert tuple(exposure.key_columns) == (
        "tenant_id",
        "correlation_id",
        "step_sequence",
    )


def test_exposure_is_tenant_scoped() -> None:
    assert _exposure().tenant_scoped


def test_exposure_can_be_read_for_one_run() -> None:
    assert topic_supports_correlation_id_filter(_exposure())


def test_exposure_orders_by_the_step_sequence_and_nothing_time_based() -> None:
    order = str(_exposure().order_by)
    assert order.replace(" ", "").lower().endswith("step_sequenceasc")
    for clock in ("started_at", "updated_at", "ingested_at", "projection_cursor"):
        assert clock not in order


def test_the_step_event_topic_is_what_the_writer_subscribes_to() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert contract["event_bus"]["subscribe_topics"] == [
        "onex.evt.omnimarket.delegation-run-step.v1"
    ]
    assert RunTraceProjectionWriter().subscribe_topics == [
        "onex.evt.omnimarket.delegation-run-step.v1"
    ]


def test_the_writer_declares_in_process_dispatch() -> None:
    assert RunTraceProjectionWriter.onex_runtime_inprocess_dispatch is True


def test_no_postgres_only_sql_is_reachable_from_the_store_neutral_path() -> None:
    source = (NODE_DIR / "handlers" / "handler_run_trace_store.py").read_text()
    for postgres_only in ("ON CONFLICT", "asyncpg", "$1", "clock_timestamp"):
        assert postgres_only not in source


def test_the_postgres_migration_declares_the_table_and_its_key() -> None:
    sql = (NODE_DIR / "migrations" / "0000_create_run_trace.sql").read_text()
    assert "CREATE TABLE IF NOT EXISTS public.run_trace" in sql
    assert "PRIMARY KEY (tenant_id, correlation_id, step_sequence)" in sql
    assert "projection_cursor BIGSERIAL" in sql


def test_the_migrations_follow_the_create_grant_force_split() -> None:
    """Only the FORCE step may be fenced, so it carries nothing a fresh lane needs."""
    migrations = NODE_DIR / "migrations"
    assert sorted(path.name for path in migrations.glob("*.sql")) == [
        "0000_create_run_trace.sql",
        "0001_grant_tenant_projection_writer_run_trace.sql",
        "0002_force_rls_run_trace.sql",
    ]
    create = (migrations / "0000_create_run_trace.sql").read_text()
    grant = (
        migrations / "0001_grant_tenant_projection_writer_run_trace.sql"
    ).read_text()
    force = (migrations / "0002_force_rls_run_trace.sql").read_text()
    assert "ENABLE ROW LEVEL SECURITY" in create
    assert "FORCE ROW LEVEL SECURITY" not in create + grant
    assert "FORCE ROW LEVEL SECURITY" in force
    assert "CREATE TABLE" not in force
    assert "TO tenant_projection_writer" not in force
    for sql in (create, force):
        assert "CREATE POLICY tenant_isolation ON public.run_trace" in sql
        assert "GRANT SELECT ON public.run_trace TO app_dashboard;" in sql
    assert "GRANT SELECT, INSERT, UPDATE" in grant
    assert "DELETE" not in grant


def test_runtime_dispatch_resolves_only_the_writer() -> None:
    contract = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    handlers = contract["handler_routing"]["handlers"]
    assert [h["handler"]["name"] for h in handlers] == ["RunTraceProjectionWriter"]
