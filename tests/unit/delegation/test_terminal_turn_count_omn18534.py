# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18534: the terminal's turn count is a typed delegation_events column.

``turn_count`` is the terminal's ``attempts_count``, the authoritative total of
inference calls. It is written from the terminal, never derived at read time,
on the delegate-skill terminal and the canonical terminal alike. A delegation
that failed before any turn stores 0, a reported 0 survives a sparse later
terminal, and a routed terminal that reported nothing leaves NULL. A run that
dispatched nothing (refused before dispatch, or not routed) stores 0 even when
its terminal omitted the count; the delegate-skill count is stored only when the
payload carried one, and a later terminal that reports a count replaces it.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models.model_prompt_size_bound import (
    load_prompt_size_bound,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.nodes.node_projection_delegation.models.model_terminal_precedence import (
    _TERMINAL_OWNED_COLUMNS,
)
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import build_projection_topic_map
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter
from tests.helpers.tenant_registry import mock_tenant_registry, seed_tenant_registry
from tests.test_omn15905_delegation_projection_writer_seam import (
    _mock_db,
    _param_by_column,
)
from tests.unit.projection.test_delegation_backend_id_and_host_omn20162 import (
    _NODE,
    _attempt,
    _canonical_terminal,
    _project,
    _skill_terminal,
)

pytestmark = pytest.mark.unit


@pytest.fixture(params=["inmemory", "sqlite"])
def db(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    if request.param == "sqlite":
        return SqliteDatabaseAdapter(tmp_path / "local.db")
    return InmemoryDatabaseAdapter()


def _single_turn_skill_terminal() -> dict[str, Any]:
    return _skill_terminal([_attempt(backend_id="local", host="h201", passed=True)])


def _escalated_skill_terminal() -> dict[str, Any]:
    """Three inference calls, one tier escalation: the two counts differ."""
    payload = _skill_terminal(
        [
            _attempt(
                backend_id="local-a",
                host="h201",
                passed=False,
                failure_class="quality_gate",
            ),
            _attempt(
                backend_id="local-a",
                host="h201",
                passed=False,
                failure_class="quality_gate",
            ),
            _attempt(backend_id="cloud", host=None, passed=True),
        ]
    )
    payload["escalation_count"] = 1
    return payload


def _refused_before_dispatch_skill_terminal() -> dict[str, Any]:
    payload = _skill_terminal([])
    payload.update(
        status="failed",
        quality_gate_passed=False,
        terminal_failure_cause="refused_before_dispatch",
        attempts_count=0,
        error_message="refused before dispatch",
    )
    return payload


def test_single_turn_and_escalated_skill_terminals_store_different_counts(
    db: Any,
) -> None:
    single = _project(_single_turn_skill_terminal(), db)
    escalated = _project(_escalated_skill_terminal(), db)
    assert single["turn_count"] == 1
    assert escalated["turn_count"] == 3
    assert single["turn_count"] != escalated["turn_count"]


def test_escalated_terminal_reports_total_calls_not_the_pre_escalation_count(
    db: Any,
) -> None:
    row = _project(_escalated_skill_terminal(), db)
    assert row["escalation_count"] == 1
    assert row["turn_count"] == 3
    assert row["turn_count"] != row["escalation_count"]


def test_failure_before_any_turn_stores_zero_not_null(db: Any) -> None:
    row = _project(_refused_before_dispatch_skill_terminal(), db)
    assert "turn_count" in row
    assert row["turn_count"] is not None
    assert row["turn_count"] == 0


@pytest.mark.parametrize("attempts_count", [0, 1, 4])
def test_canonical_terminal_stores_the_reported_count(
    db: Any, attempts_count: int
) -> None:
    row = _project(_canonical_terminal(attempts_count=attempts_count), db)
    assert row["turn_count"] == attempts_count


def test_canonical_escalated_terminal_stores_total_calls(db: Any) -> None:
    row = _project(
        _canonical_terminal(
            attempts_count=4,
            escalation_count=1,
            escalation_history=[{"model_used": "a"}, {"model_used": "b"}],
        ),
        db,
    )
    assert row["escalation_count"] == 1
    assert row["turn_count"] == 4


def test_terminal_that_reported_no_count_leaves_null(db: Any) -> None:
    row = _project(_canonical_terminal(), db)
    assert row.get("turn_count") is None


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, "2", ""])
def test_canonical_count_rejects_non_integer_or_negative_values(
    db: Any, invalid: object
) -> None:
    row = _project(_canonical_terminal(attempts_count=invalid), db)
    assert row.get("turn_count") is None


@pytest.mark.parametrize("path", ["skill", "canonical"])
@pytest.mark.parametrize("recorded", [3, 0])
def test_sparse_later_terminal_keeps_the_recorded_count(
    db: Any, path: str, recorded: int
) -> None:
    if path == "skill":
        first = (
            _escalated_skill_terminal()
            if recorded
            else _refused_before_dispatch_skill_terminal()
        )
        later = _canonical_terminal()
    else:
        first = _canonical_terminal(attempts_count=recorded)
        later = _canonical_terminal()
    _project(first, db)
    later["correlation_id"] = first["correlation_id"]
    assert _project(later, db)["turn_count"] == recorded


def test_projection_contract_exposes_the_column_and_terminal_owns_it() -> None:
    # The served value is proven through the read node below; this pins the
    # declaration itself on the parsed exposure, not on a text match.
    assert "turn_count" in build_projection_topic_map()[_TOPIC].columns
    assert "turn_count" in _TERMINAL_OWNED_COLUMNS


def test_only_migration_0069_adds_turn_count_and_ships_a_rollback() -> None:
    migrations = _NODE / "migrations"
    adders = [
        path.name
        for path in sorted(migrations.glob("*.sql"))
        if re.search(r"ADD COLUMN[^;]*\bturn_count\b", path.read_text())
    ]
    assert adders == ["0069_delegation_events_turn_count.sql"]
    sql = (migrations / adders[0]).read_text()
    assert "ADD COLUMN IF NOT EXISTS turn_count INTEGER;" in sql
    assert "NOT NULL" not in sql
    assert "DEFAULT" not in sql.upper().replace("-- ", "").split("BEGIN;")[1]
    rollback = (_NODE / "rollback" / adders[0]).read_text()
    assert "DROP COLUMN IF EXISTS turn_count" in rollback


def test_ticket_contract_names_the_omnibase_infra_copy_as_0069() -> None:
    contract = Path(__file__).resolve().parents[3] / "contracts" / "OMN-18534.yaml"
    text = contract.read_text(encoding="utf-8")
    assert "0069" in text
    assert "0068" not in text


@pytest.mark.parametrize("path", ["skill", "canonical"])
async def test_async_runner_binds_the_column_and_preserves_a_reported_zero(
    path: str,
) -> None:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    payload = (
        _refused_before_dispatch_skill_terminal()
        if path == "skill"
        else _canonical_terminal(attempts_count=0)
    )
    cid = payload["correlation_id"]
    topic = (
        runner._topic_delegate_skill_completed
        if path == "skill"
        else runner._topic_delegation_completed
    )
    assert await runner.project_event(
        topic, payload, MessageMeta(partition=0, offset=0, fallback_id=cid)
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    stored = _param_by_column(write.args)
    assert stored["turn_count"] == 0

    async def read_stored(
        query: str, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        return [stored] if query.startswith("SELECT * FROM delegation_events") else []

    mock_db.execute.reset_mock()
    mock_db.execute.side_effect = read_stored
    later = _canonical_terminal()
    later["correlation_id"] = cid
    assert await runner.project_event(
        runner._topic_delegation_completed,
        later,
        MessageMeta(partition=0, offset=1, fallback_id=cid),
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    assert _param_by_column(write.args)["turn_count"] == 0


_TOPIC = "onex.snapshot.projection.delegation.decisions.v1"


def _read_runs(db_path: Path, tenant_id: str) -> dict[str, dict[str, Any]]:
    topic_map = build_projection_topic_map()
    handler = HandlerProjectionRead(
        topic_map={_TOPIC: topic_map[_TOPIC]},
        row_source=SqliteTableRowSource(db_path),
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_TOPIC, tenant_id=tenant_id))
    )
    assert result.http_status == 200, f"served {result.response}"
    return {
        row["correlation_id"]: row
        for row in result.response["rows"]
        if "correlation_id" in row
    }


def _turn_count_column(path: Path) -> tuple[Any, ...]:
    with sqlite3.connect(path) as conn:
        row = conn.execute("PRAGMA table_info(delegation_events)").fetchall()
    columns = [col for col in row if col[1] == "turn_count"]
    assert columns, "turn_count column missing from delegation_events"
    return columns[0]


def test_read_node_serves_a_reported_zero_as_zero_and_an_unreported_count_as_null(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.db"
    db = SqliteDatabaseAdapter(path)
    refused = _project(_refused_before_dispatch_skill_terminal(), db)
    unreported = _project(_canonical_terminal(), db)
    escalated = _project(_escalated_skill_terminal(), db)
    with sqlite3.connect(path) as conn:
        (tenant_id,) = conn.execute(
            "SELECT tenant_id FROM delegation_events LIMIT 1"
        ).fetchone()
    served = _read_runs(path, tenant_id)
    for row in (
        served[refused["correlation_id"]],
        served[unreported["correlation_id"]],
        served[escalated["correlation_id"]],
    ):
        assert "turn_count" in row
    refused_served = served[refused["correlation_id"]]["turn_count"]
    assert refused_served == 0
    assert refused_served is not None
    assert type(refused_served) is int
    assert served[unreported["correlation_id"]]["turn_count"] is None
    assert served[escalated["correlation_id"]]["turn_count"] == 3


def test_a_fresh_sqlite_store_declares_turn_count_integer_nullable_before_any_terminal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "local.db"
    SqliteDatabaseAdapter(path)._connect().close()
    column = _turn_count_column(path)
    assert column[2] == "INTEGER"
    assert column[3] == 0
    assert column[4] is None


def test_a_store_created_before_the_column_gains_it_on_connect(tmp_path: Path) -> None:
    path = tmp_path / "local.db"
    db = SqliteDatabaseAdapter(path)
    db._connect().close()
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE delegation_events DROP COLUMN turn_count")
        assert not any(
            col[1] == "turn_count"
            for col in conn.execute("PRAGMA table_info(delegation_events)")
        )
    reopened = SqliteDatabaseAdapter(path)
    reopened._connect().close()
    column = _turn_count_column(path)
    # The on-connect adder declares no type; the fresh-store test pins INTEGER.
    assert column[2] == ""
    assert column[3] == 0
    assert column[4] is None
    refused = _project(_refused_before_dispatch_skill_terminal(), reopened)
    unreported = _project(_canonical_terminal(), reopened)
    assert refused["turn_count"] == 0
    assert unreported["turn_count"] is None


def test_sqlite_row_on_disk_carries_the_projected_count(tmp_path: Path) -> None:
    path = tmp_path / "local.db"
    row = _project(_escalated_skill_terminal(), SqliteDatabaseAdapter(path))
    with sqlite3.connect(path) as conn:
        (stored,) = conn.execute(
            "SELECT turn_count FROM delegation_events WHERE correlation_id = ?",
            (row["correlation_id"],),
        ).fetchone()
    assert stored == 3


def test_skill_terminal_stores_the_stated_count_not_the_history_length(
    db: Any,
) -> None:
    payload = _escalated_skill_terminal()
    payload["attempts_count"] = 5
    assert len(payload["attempts"]) == 3
    assert _project(payload, db)["turn_count"] == 5


def test_skill_terminal_with_no_count_and_no_history_stores_null(db: Any) -> None:
    payload = _skill_terminal([])
    assert "attempts_count" not in payload
    assert _project(payload, db).get("turn_count") is None


def test_skill_terminal_that_omits_the_count_keeps_the_history_length_the_model_derives(
    db: Any,
) -> None:
    payload = _escalated_skill_terminal()
    assert "attempts_count" not in payload
    assert _project(payload, db)["turn_count"] == 3


@pytest.mark.parametrize("malformed", [-1, 1.5, "many"])
def test_skill_terminal_with_a_malformed_count_is_refused_and_nothing_is_stored(
    db: Any, malformed: Any
) -> None:
    payload = _single_turn_skill_terminal()
    payload["attempts_count"] = malformed
    seed_tenant_registry(db)
    with pytest.raises(ValidationError):
        HandlerProjectionDelegation().handle({**payload, "_db": db})
    assert (
        db.query("delegation_events", {"correlation_id": payload["correlation_id"]})
        == []
    )


# --- AC3: a delegation that dispatched nothing is a zero, not an unknown ---------


async def _producer_refusal_payload() -> dict[str, Any]:
    """The terminal the delegate-skill handler itself returns for a refused run."""

    class _NeverDispatched:
        async def dispatch(self, **_kwargs: Any) -> dict[str, object]:
            raise AssertionError("a refused run must not reach a model")

    handler = HandlerDelegateSkill(dispatch_port=cast("Any", _NeverDispatched()))
    terminal = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="a" * (load_prompt_size_bound().max_prompt_bytes + 1),
            task_type="summarization",
            source="claude-code",
            correlation_id=uuid4(),
        )
    )
    assert terminal is not None
    payload = terminal.model_dump(mode="json")
    payload["tenant_id"] = _refused_before_dispatch_skill_terminal()["tenant_id"]
    return payload


async def test_producer_refusal_states_zero_and_the_row_stores_it(db: Any) -> None:
    payload = await _producer_refusal_payload()
    # Positive control: the real producer states the zero itself.
    assert payload["attempts_count"] == 0
    assert payload["terminal_failure_cause"] == "refused_before_dispatch"
    row = _project(payload, db)
    assert row["turn_count"] == 0
    assert not row["terminal_ok"]  # sqlite stores a boolean as 0


async def test_refusal_that_omits_the_count_is_stamped_zero(db: Any) -> None:
    payload = await _producer_refusal_payload()
    del payload["attempts_count"]
    row = _project(payload, db)
    assert row["terminal_failure_cause"] == "refused_before_dispatch"
    assert row["turn_count"] == 0


def test_not_routed_failure_that_reported_no_count_is_stamped_zero(db: Any) -> None:
    row = _project(_canonical_terminal(quality_passed=False, model_used=""), db)
    assert str(row["model_name"]).startswith("not routed")
    assert row["turn_count"] == 0


def test_not_routed_failure_keeps_the_count_it_reported(db: Any) -> None:
    row = _project(
        _canonical_terminal(quality_passed=False, model_used="", attempts_count=4), db
    )
    assert str(row["model_name"]).startswith("not routed")
    assert row["turn_count"] == 4


def test_routed_failure_that_reported_no_count_stays_unknown(db: Any) -> None:
    row = _project(_canonical_terminal(quality_passed=False), db)
    assert not row["terminal_ok"]  # sqlite stores a boolean as 0
    assert not str(row["model_name"]).startswith("not routed")
    assert row.get("turn_count") is None


def test_not_routed_stamp_does_not_erase_an_earlier_recorded_count(db: Any) -> None:
    first = _canonical_terminal(attempts_count=3)
    _project(first, db)
    later = _canonical_terminal(quality_passed=False, model_used="")
    later["correlation_id"] = first["correlation_id"]
    assert _project(later, db)["turn_count"] == 3


# --- two terminals that report different counts: the later report wins -----------


@pytest.mark.parametrize(
    ("first_count", "later_count"), [(3, 1), (1, 3), (4, 0), (0, 4)]
)
def test_a_later_terminal_that_reports_a_different_count_replaces_it(
    db: Any, first_count: int, later_count: int
) -> None:
    first = _canonical_terminal(attempts_count=first_count)
    later = _canonical_terminal(attempts_count=later_count)
    later["correlation_id"] = first["correlation_id"]
    _project(first, db)
    assert _project(later, db)["turn_count"] == later_count


async def test_async_runner_stamps_a_refusal_that_omits_the_count() -> None:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    payload = await _producer_refusal_payload()
    del payload["attempts_count"]
    cid = payload["correlation_id"]
    assert await runner.project_event(
        runner._topic_delegate_skill_failed,
        payload,
        MessageMeta(partition=0, offset=0, fallback_id=cid),
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    assert _param_by_column(write.args)["turn_count"] == 0
