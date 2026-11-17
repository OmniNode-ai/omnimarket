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
import hashlib
import os
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
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
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
    payload = _skill_terminal([_attempt(backend_id="local", host="h201", passed=True)])
    payload["attempts_count"] = 1
    return payload


# The escalated fixtures state their count, as the producer does: the attempt
# list holds three records, and two compliance repairs made calls it omits.
_ESCALATED_HISTORY_LENGTH = 3
_ESCALATED_TOTAL_CALLS = 5


def _escalated_skill_terminal() -> dict[str, Any]:
    """Five inference calls over three recorded attempts and one tier escalation."""
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
    payload["attempts_count"] = _ESCALATED_TOTAL_CALLS
    assert len(payload["attempts"]) == _ESCALATED_HISTORY_LENGTH
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
    assert escalated["turn_count"] == _ESCALATED_TOTAL_CALLS
    assert escalated["turn_count"] != _ESCALATED_HISTORY_LENGTH
    assert single["turn_count"] != escalated["turn_count"]


def test_escalated_terminal_reports_total_calls_not_the_pre_escalation_count(
    db: Any,
) -> None:
    row = _project(_escalated_skill_terminal(), db)
    assert row["escalation_count"] == 1
    assert row["turn_count"] == _ESCALATED_TOTAL_CALLS
    assert row["turn_count"] != row["escalation_count"] + 1


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


def _sparse_later_terminal(recorded: int) -> dict[str, Any]:
    """A later terminal that reports no usable count and so replaces nothing.

    After a recorded 0 it must also run no model: a later terminal that did run
    one contradicts the 0 and does not inherit it (see the stamped-zero tests).
    """
    if recorded:
        return _canonical_terminal()
    return _canonical_terminal(quality_passed=False, model_used="", attempts_count="x")


@pytest.mark.parametrize("path", ["skill", "canonical"])
@pytest.mark.parametrize("recorded", [_ESCALATED_TOTAL_CALLS, 0])
def test_sparse_later_terminal_keeps_the_recorded_count(
    db: Any, path: str, recorded: int
) -> None:
    if path == "skill":
        first = (
            _escalated_skill_terminal()
            if recorded
            else _refused_before_dispatch_skill_terminal()
        )
        later = _sparse_later_terminal(recorded)
    else:
        first = _canonical_terminal(attempts_count=recorded)
        later = _sparse_later_terminal(recorded)
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
    lines = contract.read_text(encoding="utf-8").splitlines()
    assert "0068" not in "\n".join(lines)
    # Line 4 is the summary: it names the migration number and the vendored copy.
    assert lines[3].startswith("summary: ")
    assert "Migration 0069 adds" in lines[3]
    assert "vendored copy is numbered 0069" in lines[3]


# The omnibase_infra copy must be this file byte for byte. The checksum is pinned
# here so a changed byte on this side is a red test on every runner. The copy
# itself lives in another repository, which a hosted test job does not check
# out (and a test that skips on an absent clone is refused by the skip ratchet),
# so a runner that has the clone names its root in ONEX_INFRA_COPY_ROOT and the
# test then reads that file: a named root that cannot be read, or whose copy
# differs by one byte, fails the test; it never skips.
_MIGRATION_0069_SHA256 = (
    "cbbdc1679ee7aa430dd807f00960122054affd15636b0bdd22baae39f646a4cb"
)
_INFRA_COPY_ROOT_ENV = "ONEX_INFRA_COPY_ROOT"
_INFRA_COPY_DIR = "docker/migrations/forward/nodes/node_projection_delegation"
_MIGRATION_0069 = "0069_delegation_events_turn_count.sql"


def _assert_infra_copy_matches(root: Path) -> None:
    """Fail unless the omnibase_infra copy under ``root`` is this migration."""
    migration = _NODE / "migrations" / _MIGRATION_0069
    infra = root / _INFRA_COPY_DIR / _MIGRATION_0069
    assert infra.is_file(), f"the omnibase_infra copy is not readable at {infra}"
    assert infra.read_bytes() == migration.read_bytes(), (
        f"the omnibase_infra copy at {infra} differs from this repository's "
        f"migration (sha256 {hashlib.sha256(infra.read_bytes()).hexdigest()} "
        f"against {_MIGRATION_0069_SHA256})"
    )


def test_migration_0069_bytes_are_pinned_for_the_omnibase_infra_copy() -> None:
    migration = _NODE / "migrations" / _MIGRATION_0069
    assert hashlib.sha256(migration.read_bytes()).hexdigest() == _MIGRATION_0069_SHA256
    named = os.environ.get(_INFRA_COPY_ROOT_ENV)
    if named:
        _assert_infra_copy_matches(Path(named))


def test_the_infra_copy_check_reads_the_copy_and_fails_on_one_changed_byte(
    tmp_path: Path,
) -> None:
    migration = _NODE / "migrations" / _MIGRATION_0069
    copy_dir = tmp_path / _INFRA_COPY_DIR
    copy_dir.mkdir(parents=True)
    copy = copy_dir / _MIGRATION_0069
    # Positive control: a byte-identical copy passes.
    copy.write_bytes(migration.read_bytes())
    _assert_infra_copy_matches(tmp_path)
    # One byte changed fails.
    changed = bytearray(migration.read_bytes())
    changed[0] ^= 0x01
    copy.write_bytes(bytes(changed))
    with pytest.raises(AssertionError, match="differs from this repository"):
        _assert_infra_copy_matches(tmp_path)
    # A named root that holds no copy fails, never passes quietly.
    copy.unlink()
    with pytest.raises(AssertionError, match="not readable"):
        _assert_infra_copy_matches(tmp_path)


def test_migration_0069_comment_states_what_the_column_stores() -> None:
    sql = (_NODE / "migrations" / "0069_delegation_events_turn_count.sql").read_text()
    header = sql.split("BEGIN;")[0]
    flat = " ".join(line.removeprefix("--").strip() for line in header.splitlines())
    assert "never derived from attempt_history" in flat
    assert "a routed terminal with no count stores NULL" in flat
    assert "every new terminal writes an integer" not in flat


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
    later = _sparse_later_terminal(0)
    later["correlation_id"] = cid
    assert await runner.project_event(
        runner._topic_delegation_failed,
        later,
        MessageMeta(partition=0, offset=1, fallback_id=cid),
    )
    (write,) = [
        call
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]
    written = _param_by_column(write.args)
    if path == "skill":
        # A stored failure owns its terminal columns: the later terminal leaves
        # the column out of the write, so the stored 0 stands.
        assert "turn_count" not in written
    else:
        assert written["turn_count"] == 0


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
    assert served[escalated["correlation_id"]]["turn_count"] == _ESCALATED_TOTAL_CALLS


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
    # The on-connect adder declares the same INTEGER type a fresh store does.
    assert column[2] == "INTEGER"
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
    assert stored == _ESCALATED_TOTAL_CALLS


def test_skill_terminal_stores_the_stated_count_not_the_history_length(
    db: Any,
) -> None:
    payload = _escalated_skill_terminal()
    payload["attempts_count"] = 7
    assert len(payload["attempts"]) == _ESCALATED_HISTORY_LENGTH
    assert _project(payload, db)["turn_count"] == 7


def test_skill_terminal_with_no_count_and_no_history_stores_null(db: Any) -> None:
    payload = _skill_terminal([])
    assert "attempts_count" not in payload
    assert _project(payload, db).get("turn_count") is None


def test_skill_terminal_that_omits_the_count_stores_null_not_the_derived_length(
    db: Any,
) -> None:
    payload = _escalated_skill_terminal()
    del payload["attempts_count"]
    # Positive control: the response model derives a count from the list, so the
    # derived value is what a lax stamp would store.
    assert (
        ModelDelegateSkillTerminalProjection.from_payload(payload).attempts_count == 3
    )
    assert _project(payload, db).get("turn_count") is None


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


# --- a zero is inferred only when no count was reported and no attempt happened ---


def _failed_not_routed(**extra: Any) -> dict[str, Any]:
    return _canonical_terminal(quality_passed=False, model_used="", **extra)


def _attempt_evidence() -> dict[str, Any]:
    return {
        "escalation_history": [
            {"model_used": "a", "tier": "local"},
            {"model_used": "b", "tier": "cheap_cloud"},
        ]
    }


@pytest.mark.parametrize("malformed", ["many", -1, 1.5, True])
@pytest.mark.parametrize(
    "shape",
    [
        pytest.param({}, id="not_routed"),
        pytest.param(
            {"terminal_failure_cause": "refused_before_dispatch"}, id="refused"
        ),
    ],
)
def test_a_malformed_count_is_never_replaced_by_the_inferred_zero(
    db: Any, malformed: object, shape: dict[str, Any]
) -> None:
    row = _project(_failed_not_routed(attempts_count=malformed, **shape), db)
    # Positive control: the row is one the stamp would have zeroed.
    assert str(row["model_name"]).startswith("not routed")
    assert row.get("turn_count") is None


@pytest.mark.parametrize(
    "evidence",
    [
        pytest.param(_attempt_evidence(), id="attempt_history"),
        pytest.param({"prompt_tokens": 12}, id="prompt_tokens"),
        pytest.param({"completion_tokens": 7}, id="completion_tokens"),
    ],
)
def test_a_refused_failure_with_attempt_evidence_and_no_count_stores_null(
    db: Any, evidence: dict[str, Any]
) -> None:
    row = _project(
        _failed_not_routed(
            terminal_failure_cause="refused_before_dispatch", **evidence
        ),
        db,
    )
    assert row.get("turn_count") is None


def test_the_attempt_evidence_rule_is_what_withholds_the_zero(db: Any) -> None:
    # Positive control for the cases above.
    row = _project(_failed_not_routed(**_attempt_evidence()), db)
    assert str(row["model_name"]).startswith("not routed")
    assert row.get("turn_count") is None
    # Without the record the same failure is stamped, so the NULL is the rule.
    assert _project(_failed_not_routed(), db)["turn_count"] == 0


def test_a_later_sparse_routed_terminal_does_not_keep_a_stamped_zero(db: Any) -> None:
    first = _failed_not_routed()
    assert _project(first, db)["turn_count"] == 0  # stamped, never reported
    later = _canonical_terminal()
    later["correlation_id"] = first["correlation_id"]
    assert _project(later, db).get("turn_count") is None


def test_a_later_routed_terminal_that_reports_a_count_replaces_a_stamped_zero(
    db: Any,
) -> None:
    first = _failed_not_routed()
    _project(first, db)
    later = _canonical_terminal(attempts_count=2)
    later["correlation_id"] = first["correlation_id"]
    assert _project(later, db)["turn_count"] == 2


def _insert_writes(mock_db: Any) -> list[dict[str, Any]]:
    return [
        _param_by_column(call.args)
        for call in mock_db.execute.await_args_list
        if str(call.args[0]).startswith("INSERT INTO delegation_events")
    ]


async def _async_project(
    runner: Any, mock_db: Any, topic: str, payload: dict[str, Any], offset: int
) -> dict[str, Any]:
    cid = payload["correlation_id"]
    mock_db.execute.reset_mock()
    assert await runner.project_event(
        topic, payload, MessageMeta(partition=0, offset=offset, fallback_id=cid)
    )
    (write,) = _insert_writes(mock_db)
    return write


def _async_runner() -> tuple[DelegationProjectionRunner, Any]:
    runner = DelegationProjectionRunner()
    mock_db = _mock_db()
    mock_tenant_registry(mock_db)
    runner._db = mock_db
    return runner, mock_db


async def _stored_then_later(
    first: dict[str, Any], later: dict[str, Any]
) -> dict[str, Any]:
    """Write ``first`` on the async runner, then ``later`` over the stored row."""
    runner, mock_db = _async_runner()
    later["correlation_id"] = first["correlation_id"]
    stored = await _async_project(
        runner, mock_db, runner._topic_delegation_completed, first, 0
    )

    async def read_stored(
        query: str, *args: object, **kwargs: object
    ) -> list[dict[str, object]]:
        return [stored] if query.startswith("SELECT * FROM delegation_events") else []

    mock_db.execute.side_effect = read_stored
    return await _async_project(
        runner, mock_db, runner._topic_delegation_failed, later, 1
    )


async def test_async_later_refused_terminal_keeps_an_earlier_count() -> None:
    written = await _stored_then_later(
        _canonical_terminal(attempts_count=4),
        _failed_not_routed(terminal_failure_cause="refused_before_dispatch"),
    )
    # The stamp runs after the stored row is merged in, so the earlier count is
    # in the row by then and the refusal stamps no zero over it.
    assert written["turn_count"] == 4


async def test_async_later_not_routed_terminal_keeps_an_earlier_count() -> None:
    written = await _stored_then_later(
        _canonical_terminal(attempts_count=4), _failed_not_routed()
    )
    assert written["turn_count"] == 4


async def test_async_later_terminal_that_reports_a_count_replaces_the_earlier_one() -> (
    None
):
    # The intent: the later terminal's own report wins, a 0 over a 4 included.
    # A terminal states the total calls it made for the whole delegation, and a
    # zero is a measured report ("no call was made"), so nothing here treats a
    # reported zero as less authoritative than a positive count.
    written = await _stored_then_later(
        _canonical_terminal(attempts_count=4), _canonical_terminal(attempts_count=0)
    )
    assert written["turn_count"] == 0


async def test_async_later_sparse_routed_terminal_does_not_keep_a_stamped_zero() -> (
    None
):
    written = await _stored_then_later(_failed_not_routed(), _canonical_terminal())
    # The write names the column, as NULL: omitting it would leave the zero.
    assert "turn_count" in written
    assert written["turn_count"] is None


async def test_async_malformed_count_on_a_not_routed_failure_stores_no_zero() -> None:
    runner, mock_db = _async_runner()
    write = await _async_project(
        runner,
        mock_db,
        runner._topic_delegation_failed,
        _failed_not_routed(attempts_count="many"),
        0,
    )
    assert write.get("turn_count") is None


# --- the skill path stores the count the real producer emitted --------------------


def _escalated_producer_payload(**overrides: Any) -> dict[str, Any]:
    """Run HandlerDelegateSkill over a frozen escalated run, with compliance repairs.

    The frozen dispatch result climbed from a local rung to a cloud rung. The
    port also reports the repair calls it made on the way (``compliance_attempts``)
    that no attempt record carries, so the producer's total exceeds the list.
    """
    from tests.test_omn17979_escalated_accept_terminal_seam import (
        _load_fixture,
        _run_producer,
    )

    fixture = _load_fixture()
    result = dict(fixture["dispatch_result"])
    result.pop("attempts_count", None)
    result.update(overrides)
    terminal = _run_producer(
        result,
        correlation_id=fixture["correlation_id"],
        prompt=fixture["prompt_text"],
        task_type=fixture["task_type"],
    )
    payload = terminal.model_dump(mode="json")
    payload["tenant_id"] = _escalated_skill_terminal()["tenant_id"]
    return payload


@pytest.mark.parametrize(
    ("overrides", "total"),
    [
        pytest.param({"compliance_attempts": 6}, 6, id="repairs_reported_by_the_port"),
        pytest.param({"attempts_count": 7}, 7, id="count_stated_by_the_port"),
    ],
)
def test_the_escalated_producer_count_exceeds_its_attempt_list_and_the_row_stores_it(
    db: Any, overrides: dict[str, Any], total: int
) -> None:
    payload = _escalated_producer_payload(**overrides)
    # No fixture count is copied in: the producer emitted this value itself.
    assert payload["escalation_count"] >= 1
    assert payload["attempts_count"] == total
    assert payload["attempts_count"] != len(payload["attempts"])
    assert payload["attempts_count"] > len(payload["attempts"])
    row = _project(payload, db)
    assert row["turn_count"] == total
    assert row["turn_count"] != len(payload["attempts"])
    # The producer's own model_dump always emits the key, so a reported count
    # is the producer's value, and the canonical path agrees on it.
    canonical = _project(_canonical_terminal(attempts_count=total), db)
    assert canonical["turn_count"] == row["turn_count"]


def test_a_skill_terminal_a_dispatch_port_publishes_without_a_count_stores_null(
    db: Any,
) -> None:
    payload = _escalated_producer_payload(compliance_attempts=6)
    del payload["attempts_count"]
    assert len(payload["attempts"]) >= 2
    assert _project(payload, db).get("turn_count") is None


@pytest.mark.parametrize("not_a_count", ["2", True, 2.0])
def test_the_skill_count_is_strict_and_a_string_or_a_bool_is_refused(
    db: Any, not_a_count: object
) -> None:
    payload = _single_turn_skill_terminal()
    payload["attempts_count"] = not_a_count
    seed_tenant_registry(db)
    with pytest.raises(ValidationError):
        HandlerProjectionDelegation().handle({**payload, "_db": db})
    assert (
        db.query("delegation_events", {"correlation_id": payload["correlation_id"]})
        == []
    )


def test_the_skill_count_has_no_camel_case_alias_the_reported_check_would_miss(
    db: Any,
) -> None:
    payload = _escalated_skill_terminal()
    del payload["attempts_count"]
    payload["attemptsCount"] = 9
    terminal = ModelDelegateSkillTerminalProjection.from_payload(payload)
    # Neither the model nor the reported check reads the alias, so they agree.
    assert terminal.attempts_count == _ESCALATED_HISTORY_LENGTH
    assert terminal.turn_count_reported is False
    assert _project(payload, db).get("turn_count") is None


# --- finding 3: a refusal over a routed row is not a zero --------------------------


def _skill_refusal_without_a_count() -> dict[str, Any]:
    """The producer's own refusal (it names no model), with the count left out."""
    payload = asyncio.run(_producer_refusal_payload())
    del payload["attempts_count"]
    return payload


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(
            lambda: _failed_not_routed(
                terminal_failure_cause="refused_before_dispatch"
            ),
            id="canonical_refusal",
        ),
        pytest.param(_skill_refusal_without_a_count, id="skill_refusal"),
    ],
)
def test_a_no_count_refusal_over_a_routed_row_with_no_tokens_stays_unknown(
    db: Any, refusal: Any
) -> None:
    first = _canonical_terminal()
    stored = _project(first, db)
    # The stored row names a routed model, holds no count, no tokens and no history.
    assert stored["model_name"] == "qwen3.8-27b"
    assert stored.get("turn_count") is None
    assert not stored.get("tokens_input")
    assert not stored.get("tokens_output")
    later = refusal()
    later["correlation_id"] = first["correlation_id"]
    row = _project(later, db)
    assert row["terminal_failure_cause"] == "refused_before_dispatch"
    assert row.get("turn_count") is None


@pytest.mark.parametrize(
    "refusal",
    [
        pytest.param(
            lambda: _failed_not_routed(
                terminal_failure_cause="refused_before_dispatch"
            ),
            id="canonical_refusal",
        ),
        pytest.param(_skill_refusal_without_a_count, id="skill_refusal"),
    ],
)
def test_a_no_count_refusal_over_an_unnamed_row_is_still_stamped_zero(
    db: Any, refusal: Any
) -> None:
    # Positive control for the routed case above: the same refusal, with no
    # routed row behind it, is the zero the stamp exists to write.
    assert _project(refusal(), db)["turn_count"] == 0


async def test_async_no_count_refusal_over_a_routed_row_stays_unknown() -> None:
    written = await _stored_then_later(
        _canonical_terminal(),
        _failed_not_routed(terminal_failure_cause="refused_before_dispatch"),
    )
    assert written.get("turn_count") is None


# --- finding 4: the rule for a recorded zero under a later no-count terminal -------
#
# Decided. A zero a failure terminal reports travels with its typed failure cause
# (a refusal before dispatch), and a stored typed failure owns its terminal
# columns, so a later no-count terminal, routed or not, leaves that zero. A zero
# the writer only stamped (a run that named no model and reported no count) is an
# inference, so a later no-count terminal that names the model it ran replaces it
# with NULL; one that ran no model keeps it. The upsert writes the NULL as given.


def _canonical_refusal_reporting_zero() -> dict[str, Any]:
    return _canonical_terminal(
        quality_passed=False,
        model_used="",
        attempts_count=0,
        terminal_failure_cause="refused_before_dispatch",
    )


_FIRST_TERMINALS_RECORDING_ZERO: dict[str, Any] = {
    "reported_by_the_skill_producer": lambda: asyncio.run(_producer_refusal_payload()),
    "reported_by_a_canonical_refusal": _canonical_refusal_reporting_zero,
    "stamped_by_the_writer": _failed_not_routed,
}


@pytest.mark.parametrize(
    ("first_kind", "later_names_a_model", "expected"),
    [
        ("reported_by_the_skill_producer", False, 0),
        ("reported_by_the_skill_producer", True, 0),
        ("reported_by_a_canonical_refusal", False, 0),
        ("reported_by_a_canonical_refusal", True, 0),
        ("stamped_by_the_writer", False, 0),
        ("stamped_by_the_writer", True, None),
    ],
)
def test_a_reported_zero_survives_a_later_no_count_terminal_and_a_stamped_zero_yields(
    db: Any, first_kind: str, later_names_a_model: bool, expected: int | None
) -> None:
    first = _FIRST_TERMINALS_RECORDING_ZERO[first_kind]()
    assert _project(first, db)["turn_count"] == 0
    later = _canonical_terminal() if later_names_a_model else _sparse_later_terminal(0)
    later["correlation_id"] = first["correlation_id"]
    assert _project(later, db).get("turn_count") == expected


@pytest.mark.parametrize(
    ("first_kind", "expected"),
    [("reported_by_a_canonical_refusal", 0), ("stamped_by_the_writer", None)],
)
async def test_async_a_later_routed_no_count_terminal_keeps_a_reported_zero_only(
    first_kind: str, expected: int | None
) -> None:
    first = _FIRST_TERMINALS_RECORDING_ZERO[first_kind]()
    written = await _stored_then_later(first, _canonical_terminal())
    # The column is named in the write either way, so the stored value is replaced
    # by exactly what the rule decided: the reported 0 kept, or NULL.
    assert "turn_count" in written
    assert written["turn_count"] == expected


async def test_async_a_later_not_routed_terminal_keeps_a_stamped_zero() -> None:
    written = await _stored_then_later(_failed_not_routed(), _sparse_later_terminal(0))
    assert written["turn_count"] == 0


def test_the_ticket_contract_states_the_decided_rule_for_a_recorded_zero() -> None:
    contract = Path(__file__).resolve().parents[3] / "contracts" / "OMN-18534.yaml"
    lines = contract.read_text(encoding="utf-8").splitlines()
    for number in (4, 48):  # the summary and the AC3 evidence description
        text = lines[number - 1]
        assert "ran no model keeps a recorded" in text, number
        assert "reported zero" in text, number
        assert "stamped zero" in text, number
        assert "names a model it ran" in text, number
        assert "NULL" in text, number


# --- the read node refuses, never serves a guess, while the column is missing ------


def test_the_read_node_refuses_the_exposure_while_the_relation_lacks_turn_count(
    tmp_path: Path,
) -> None:
    """Settles the deploy order: the infra copy of 0069 is applied before this code.

    A relation without ``turn_count`` makes the read node refuse the exposure that
    declares it (``projection_column_missing``); it serves no row and no NULL.
    """
    path = tmp_path / "local.db"
    row = _project(_escalated_skill_terminal(), SqliteDatabaseAdapter(path))
    with sqlite3.connect(path) as conn:
        (tenant_id,) = conn.execute(
            "SELECT tenant_id FROM delegation_events LIMIT 1"
        ).fetchone()
        conn.execute("ALTER TABLE delegation_events DROP COLUMN turn_count")
    handler = HandlerProjectionRead(
        topic_map={_TOPIC: build_projection_topic_map()[_TOPIC]},
        row_source=SqliteTableRowSource(path),
    )
    result = asyncio.run(
        handler.handle(ModelProjectionReadRequest(topic=_TOPIC, tenant_id=tenant_id))
    )
    assert result.http_status != 200, row["correlation_id"]
    assert "projection_column_missing" in str(result.response)
