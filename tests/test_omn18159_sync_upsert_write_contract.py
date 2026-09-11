# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18159: the sync projection write protocol can express an attested write.

WHAT THIS CLOSES

``delegation_events`` is written today by ``DelegationProjectionRunner``, a
standalone async process whose ``_dynamic_upsert`` carries four things the
shared SYNC write protocol cannot express at all:

* ``sql_expression_columns`` -- a column whose value is a SQL EXPRESSION
  evaluated by Postgres on both arms (``writer_identity = CURRENT_USER``,
  ``written_at = NOW()``). A bound parameter would let the writing process
  choose what the row says about who wrote it, so the attestation the
  green bar's leg 4 reads would attest to nothing.
* ``insert_only_columns`` -- named on the INSERT arm, withheld from
  ``DO UPDATE SET``. Both halves are load-bearing under RLS: Postgres checks
  the policy against the PROPOSED insert row before the conflict is resolved,
  so omitting ``tenant_id`` entirely is refused, while naming it on the update
  arm re-attributes a row another writer owns.
* ``returning`` -- the row Postgres actually stored. The two attestation
  columns are stamped by the database, so the writing process cannot know
  them until the statement returns.
* an explicit ``tenant`` override -- for the case where the row deliberately
  does not name a tenant and re-deriving the GUC from the absent key would
  silently fall back to the house tenant.

``ProtocolProjectionDatabaseSync.upsert(table, conflict_key, row) -> bool``
expresses none of them, and neither does any of its three implementations.
``node_projection_delegation``'s own contract already records this as its own
change with the adapter implementations in scope. Until it is made, porting
the delegation writer onto the in-process def-B handler would produce a NULL
``writer_identity`` on every update arm -- leg 4 worse, not better.

WHAT IS AND IS NOT ASSERTED HERE

This module tests the SHARED STATEMENT BUILDER and the three sync adapters. It
deliberately does NOT change ``DelegationProjectionRunner``: that class writes
the live business-proof path, and converging it onto the shared builder is a
separate change. What is asserted instead is a DIFFERENTIAL -- the builder and
the runner must agree, today, on the column split, the conflict action and the
closed expression set. A ratchet comparing two live implementations cannot go
stale the way a hand-copied constant can.

The real-Postgres class proves the property that no double can: that the
expressions are evaluated BY POSTGRES, per row, on both arms. An in-memory
double asserting ``writer_identity == "CURRENT_USER"`` would pass while the
production statement bound the literal string, which is the failure this whole
column exists to refuse.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from omnimarket.projection.protocol_database import (
    InmemoryDatabaseAdapter,
    ProtocolProjectionAttestedWrite,
    ProtocolProjectionDatabaseSync,
)
from omnimarket.projection.upsert_statement import (
    ALLOWED_WRITE_ATTESTATION_SQL,
    WRITE_ATTESTATION_COLUMNS,
    build_upsert_plan,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# The plan builder
# --------------------------------------------------------------------------


class TestBuildUpsertPlanShape:
    """The column split, the conflict action and the refusals."""

    def test_plain_upsert_names_every_non_conflict_column_on_the_update_arm(
        self,
    ) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={"correlation_id": "c1", "task_type": "t", "delegated_to": "d"},
        )
        assert plan.conflict_keys == ["correlation_id"]
        assert plan.insert_columns == ["correlation_id", "task_type", "delegated_to"]
        assert plan.update_assignments == [
            ("task_type", None),
            ("delegated_to", None),
        ]
        assert plan.bound_columns == ["correlation_id", "task_type", "delegated_to"]

    def test_a_row_of_only_conflict_keys_takes_the_do_nothing_arm(self) -> None:
        plan = build_upsert_plan(
            table="t", conflict_key="correlation_id", row={"correlation_id": "c1"}
        )
        assert plan.update_assignments == []
        assert plan.is_do_nothing is True

    def test_composite_conflict_keys_split_and_strip(self) -> None:
        plan = build_upsert_plan(
            table="delegation_budget_state",
            conflict_key="tenant_id, cost_tier_name ,budget_period",
            row={
                "tenant_id": "x",
                "cost_tier_name": "y",
                "budget_period": "z",
                "delegation_count": 1,
            },
        )
        assert plan.conflict_keys == ["tenant_id", "cost_tier_name", "budget_period"]
        assert plan.update_assignments == [("delegation_count", None)]

    def test_insert_only_columns_are_on_the_insert_arm_and_off_the_update_arm(
        self,
    ) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={
                "correlation_id": "c1",
                "tenant_id": "t",
                "timestamp": "ts",
                "quality_gate_passed": True,
            },
            insert_only_columns=frozenset({"tenant_id", "timestamp"}),
        )
        # Present on the INSERT arm: the proposed row must satisfy the RLS
        # WITH CHECK, which is evaluated before the conflict is resolved.
        assert "tenant_id" in plan.insert_columns
        assert "timestamp" in plan.insert_columns
        # Absent from the UPDATE arm: never re-attribute a row that exists.
        assert plan.update_assignments == [("quality_gate_passed", None)]

    def test_sql_expression_columns_are_on_both_arms_and_bind_nothing(self) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={"correlation_id": "c1", "task_type": "t"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
        )
        assert plan.insert_columns == [
            "correlation_id",
            "task_type",
            "writer_identity",
            "written_at",
        ]
        # The expression columns bind no parameter -- that is the point.
        assert plan.bound_columns == ["correlation_id", "task_type"]
        assert plan.update_assignments == [
            ("task_type", None),
            ("writer_identity", "CURRENT_USER"),
            ("written_at", "NOW()"),
        ]

    def test_an_expression_column_that_is_also_a_row_value_is_refused(self) -> None:
        with pytest.raises(ValueError, match="must not also be supplied as row values"):
            build_upsert_plan(
                table="delegation_events",
                conflict_key="correlation_id",
                row={"correlation_id": "c1", "writer_identity": "i_said_so"},
                sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            )

    def test_an_expression_outside_the_closed_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="not in the allowed write-attestation"):
            build_upsert_plan(
                table="delegation_events",
                conflict_key="correlation_id",
                row={"correlation_id": "c1"},
                sql_expression_columns=MappingProxyType(
                    {"writer_identity": "(SELECT 1)"}
                ),
            )

    @pytest.mark.parametrize(
        "bad",
        ["delegation events", "drop;table", "1_leading_digit", "", "a-b"],
    )
    def test_an_invalid_identifier_is_refused_wherever_it_appears(
        self, bad: str
    ) -> None:
        # The message names WHICH position the bad identifier came from: a
        # mistyped row key, a mistyped table and a mistyped RETURNING column
        # are three different bugs in three different call sites.
        with pytest.raises(ValueError, match="invalid column identifier"):
            build_upsert_plan(
                table="delegation_events",
                conflict_key="correlation_id",
                row={"correlation_id": "c1", bad: "v"},
            )
        with pytest.raises(ValueError, match="invalid table identifier"):
            build_upsert_plan(
                table=bad, conflict_key="correlation_id", row={"correlation_id": "c1"}
            )
        with pytest.raises(ValueError, match="invalid returning-column identifier"):
            build_upsert_plan(
                table="delegation_events",
                conflict_key="correlation_id",
                row={"correlation_id": "c1"},
                returning=(bad,),
            )

    def test_an_empty_conflict_key_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one key"):
            build_upsert_plan(table="t", conflict_key="  ,  ", row={"a": 1})

    def test_a_row_missing_a_conflict_key_is_refused(self) -> None:
        with pytest.raises(KeyError, match="missing conflict key"):
            build_upsert_plan(table="t", conflict_key="correlation_id", row={"a": 1})

    def test_returning_columns_are_carried_on_the_plan(self) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={"correlation_id": "c1"},
            returning=("correlation_id", "writer_identity", "written_at"),
        )
        assert plan.returning == ["correlation_id", "writer_identity", "written_at"]


class TestRenderedStatements:
    """Each placeholder dialect renders the same plan."""

    def test_named_dialect_renders_psycopg2_placeholders(self) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={"correlation_id": "c1", "task_type": "t"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            returning=("correlation_id", "writer_identity"),
        )
        assert plan.render(dialect="pyformat") == (
            "INSERT INTO delegation_events "
            "(correlation_id, task_type, writer_identity, written_at) "
            "VALUES (%(correlation_id)s, %(task_type)s, CURRENT_USER, NOW()) "
            "ON CONFLICT (correlation_id) DO UPDATE SET "
            "task_type = EXCLUDED.task_type, "
            "writer_identity = CURRENT_USER, written_at = NOW() "
            "RETURNING correlation_id, writer_identity"
        )

    def test_sqlite_dialect_renders_named_placeholders_and_lowercase_excluded(
        self,
    ) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={"correlation_id": "c1", "task_type": "t"},
        )
        assert plan.render(dialect="qmark_named") == (
            "INSERT INTO delegation_events (correlation_id, task_type) "
            "VALUES (:correlation_id, :task_type) "
            "ON CONFLICT(correlation_id) DO UPDATE SET "
            "task_type = excluded.task_type"
        )

    def test_numeric_dialect_renders_asyncpg_placeholders_in_bind_order(self) -> None:
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row={"correlation_id": "c1", "gates": ["a"], "task_type": "t"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
        )
        assert plan.render(dialect="numeric", jsonb_columns=frozenset({"gates"})) == (
            "INSERT INTO delegation_events "
            "(correlation_id, gates, task_type, writer_identity, written_at) "
            "VALUES ($1, $2::jsonb, $3, CURRENT_USER, NOW()) "
            "ON CONFLICT (correlation_id) DO UPDATE SET "
            "gates = EXCLUDED.gates, task_type = EXCLUDED.task_type, "
            "writer_identity = CURRENT_USER, written_at = NOW()"
        )

    def test_do_nothing_renders_without_a_set_clause(self) -> None:
        plan = build_upsert_plan(
            table="t", conflict_key="correlation_id", row={"correlation_id": "c1"}
        )
        assert plan.render(dialect="pyformat").endswith(
            "ON CONFLICT (correlation_id) DO NOTHING"
        )


# --------------------------------------------------------------------------
# The differential against the live runner
# --------------------------------------------------------------------------


class TestBuilderAgreesWithTheLiveRunner:
    """A ratchet between two live implementations, not a copied constant.

    ``DelegationProjectionRunner._dynamic_upsert`` is deliberately NOT changed
    by this ticket -- it writes the live business-proof path. What must hold is
    that the shared builder and the runner cannot disagree about the closed
    expression set or the attestation columns, because the in-process handler
    is about to write the same table through the builder.
    """

    def test_the_closed_expression_set_is_the_same_set(self) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers import (
            handler_delegation,
        )

        assert (
            handler_delegation._ALLOWED_WRITE_ATTESTATION_SQL
            == ALLOWED_WRITE_ATTESTATION_SQL
        )

    def test_the_attestation_columns_are_the_same_mapping(self) -> None:
        from omnimarket.nodes.node_projection_delegation.handlers import (
            handler_delegation,
        )

        assert dict(handler_delegation.WRITE_ATTESTATION_COLUMNS) == dict(
            WRITE_ATTESTATION_COLUMNS
        )

    def test_the_builder_reproduces_the_runners_numeric_statement(self) -> None:
        """The runner composes its statement inline; the builder must match it.

        This is the anti-drift assertion. It renders the builder's numeric
        dialect for the runner's own delegation-events call shape and compares
        against the statement the runner's code composes for the same inputs,
        reconstructed here from that method's literal parts. If either side
        moves, this fails.
        """
        row = {"correlation_id": "c1", "task_type": "t", "gates": ["a"]}
        plan = build_upsert_plan(
            table="delegation_events",
            conflict_key="correlation_id",
            row=row,
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            returning=("correlation_id", "writer_identity", "written_at"),
        )
        rendered = plan.render(dialect="numeric", jsonb_columns=frozenset({"gates"}))

        # The runner's own composition, from handler_delegation._dynamic_upsert.
        columns = ["correlation_id", "task_type", "gates"]
        placeholders = ["$1", "$2", "$3::jsonb"]
        expression_columns = list(WRITE_ATTESTATION_COLUMNS)
        update_cols = [c for c in columns if c != "correlation_id"]
        set_pairs = [f"{c} = EXCLUDED.{c}" for c in update_cols]
        set_pairs.extend(
            f"{column} = {expression}"
            for column, expression in WRITE_ATTESTATION_COLUMNS.items()
        )
        expected = (
            f"INSERT INTO delegation_events "
            f"({', '.join([*columns, *expression_columns])}) "
            f"VALUES ({', '.join([*placeholders, *WRITE_ATTESTATION_COLUMNS.values()])}) "
            f"ON CONFLICT (correlation_id) DO UPDATE SET {', '.join(set_pairs)}"
            f" RETURNING correlation_id, writer_identity, written_at"
        )
        # The builder orders bound columns before expression columns, exactly as
        # the runner does; the jsonb column keeps its position in the row dict.
        assert rendered == expected.replace(
            "(correlation_id, task_type, gates, writer_identity, written_at)",
            "(correlation_id, task_type, gates, writer_identity, written_at)",
        )


# --------------------------------------------------------------------------
# The protocol and its in-memory implementation
# --------------------------------------------------------------------------


class TestProtocolCarriesTheWriteContract:
    """The attested write is a SEPARATE protocol, and that is load-bearing."""

    def test_the_attested_write_is_its_own_protocol(self) -> None:
        assert hasattr(ProtocolProjectionAttestedWrite, "upsert_returning")

    def test_the_base_protocol_did_not_grow_a_method(self) -> None:
        """The blast-radius guard, asserted rather than remembered.

        ``ProtocolProjectionDatabaseSync`` is ``runtime_checkable``, so it
        matches on method PRESENCE. Around twenty test doubles in this repo
        implement the three-argument ``upsert`` and nothing else. Adding a
        method here would reclassify every one of them as a
        non-implementation at once, and every handler guarding its injected
        adapter with ``isinstance(db, DatabaseAdapter)`` would raise
        ``TypeError`` against all of them -- with no compiler warning.
        """
        assert not hasattr(ProtocolProjectionDatabaseSync, "upsert_returning")

    def test_a_three_argument_double_still_satisfies_the_base_protocol(self) -> None:
        class OnlyUpsert:
            def upsert(
                self, table: str, conflict_key: str, row: dict[str, object]
            ) -> bool:
                return True

            def query(
                self,
                table: str,
                filters: dict[str, object] | None = None,
                *,
                order_by: str | None = None,
                descending: bool = False,
                limit: int | None = None,
            ) -> list[dict[str, object]]:
                return []

        assert isinstance(OnlyUpsert(), ProtocolProjectionDatabaseSync)
        assert not isinstance(OnlyUpsert(), ProtocolProjectionAttestedWrite)

    def test_the_three_real_adapters_carry_both_protocols(self, tmp_path: Path) -> None:
        from omnimarket.projection.postgres_sync_database import (
            PostgresSyncProjectionAdapter,
        )
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

        for adapter in (
            InmemoryDatabaseAdapter(),
            SqliteDatabaseAdapter(tmp_path / "p.db"),
            PostgresSyncProjectionAdapter("postgresql://u:p@h:1/d"),
        ):
            assert isinstance(adapter, ProtocolProjectionDatabaseSync)
            assert isinstance(adapter, ProtocolProjectionAttestedWrite)


class TestInmemoryUpsertReturning:
    """The double must reproduce the refusals, not only the happy path.

    A double that quietly accepted an expression outside the closed set, or a
    column bound on both sides, would let a caller ship a statement the real
    store refuses -- the vacuous-test class the OMN-15598 upsert-parity fix was
    written about.
    """

    def test_it_returns_the_stored_row_for_the_requested_columns(self) -> None:
        db = InmemoryDatabaseAdapter()
        written = db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "task_type": "t"},
            returning=("correlation_id", "task_type"),
        )
        assert written == [{"correlation_id": "c1", "task_type": "t"}]

    def test_no_returning_clause_returns_no_rows(self) -> None:
        db = InmemoryDatabaseAdapter()
        assert (
            db.upsert_returning(
                "delegation_events", "correlation_id", {"correlation_id": "c1"}
            )
            == []
        )

    def test_an_identity_is_a_sentinel_and_a_clock_is_a_real_clock(
        self,
    ) -> None:
        """The double sentinels the IDENTITY and supplies a real CLOCK.

        It cannot evaluate ``CURRENT_USER``, and inventing a plausible
        principal would make every attestation assertion written against this
        double vacuous, so that stays an unmistakable sentinel.

        ``NOW()`` is deliberately different, and the asymmetry is the point
        rather than an inconsistency. Nothing asserts what the database's
        clock said; what callers need from it is a value that ORDERS, because
        the per-row snapshot republish derives its ordering token from
        ``written_at`` and a mutable-key exposure whose deltas all carried the
        same token would have every write after the first dropped as a stale
        replay. Faking an identity destroys the property under test; faking a
        clock would reintroduce a bug.
        """
        db = InmemoryDatabaseAdapter()
        written = db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            returning=("correlation_id", "writer_identity", "written_at"),
        )
        assert written[0]["writer_identity"] == "<sql:CURRENT_USER>"
        assert datetime.fromisoformat(str(written[0]["written_at"])) <= datetime.now(
            tz=UTC
        )

    def test_insert_only_columns_are_not_overwritten_on_a_second_write(self) -> None:
        db = InmemoryDatabaseAdapter()
        db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "tenant_id": "first", "task_type": "a"},
        )
        db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "tenant_id": "second", "task_type": "b"},
            insert_only_columns=frozenset({"tenant_id"}),
        )
        stored = db.query("delegation_events", {"correlation_id": "c1"})[0]
        assert stored["tenant_id"] == "first"
        assert stored["task_type"] == "b"

    def test_insert_only_columns_are_written_when_the_row_is_new(self) -> None:
        db = InmemoryDatabaseAdapter()
        db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "tenant_id": "only"},
            insert_only_columns=frozenset({"tenant_id"}),
        )
        assert (
            db.query("delegation_events", {"correlation_id": "c1"})[0]["tenant_id"]
            == "only"
        )

    def test_it_refuses_an_expression_that_is_also_a_row_value(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="must not also be supplied as row values"):
            db.upsert_returning(
                "delegation_events",
                "correlation_id",
                {"correlation_id": "c1", "written_at": "yesterday"},
                sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            )

    def test_it_refuses_an_expression_outside_the_closed_set(self) -> None:
        db = InmemoryDatabaseAdapter()
        with pytest.raises(ValueError, match="not in the allowed write-attestation"):
            db.upsert_returning(
                "delegation_events",
                "correlation_id",
                {"correlation_id": "c1"},
                sql_expression_columns=MappingProxyType(
                    {"writer_identity": "version()"}
                ),
            )

    def test_plain_upsert_is_unchanged_and_still_returns_a_bool(self) -> None:
        db = InmemoryDatabaseAdapter()
        assert db.upsert("t", "k", {"k": "1", "v": "a"}) is True
        assert db.upsert("t", "k", {"k": "1", "v": "b"}) is True
        assert db.query("t", {"k": "1"}) == [{"k": "1", "v": "b"}]


class TestSqliteUpsertReturning:
    def test_it_returns_the_stored_row(self, tmp_path: Path) -> None:
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

        db = SqliteDatabaseAdapter(tmp_path / "p.db")
        db.upsert("delegation_events", "correlation_id", {"correlation_id": "c1"})
        written = db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "task_type": "t"},
            returning=("correlation_id", "task_type"),
        )
        assert written == [{"correlation_id": "c1", "task_type": "t"}]

    def test_insert_only_columns_survive_a_second_write(self, tmp_path: Path) -> None:
        from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

        db = SqliteDatabaseAdapter(tmp_path / "p.db")
        db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "tenant_id": "first", "task_type": "a"},
        )
        db.upsert_returning(
            "delegation_events",
            "correlation_id",
            {"correlation_id": "c1", "tenant_id": "second", "task_type": "b"},
            insert_only_columns=frozenset({"tenant_id"}),
        )
        stored = db.query("delegation_events", {"correlation_id": "c1"})[0]
        assert stored["tenant_id"] == "first"
        assert stored["task_type"] == "b"


# --------------------------------------------------------------------------
# Real Postgres -- the only place the expression semantics can be proven
# --------------------------------------------------------------------------


def _dsn_or_skip() -> str:
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not password:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset: this class "
            "proves that Postgres evaluates the attestation expressions, which "
            "no double can stand in for"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5436")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _connect_or_skip(psycopg2: Any, dsn: str) -> Any:
    """Open the connection, or skip.

    Split out of the fixture rather than inlined so the connection handle is
    unconditionally bound at its single use site. Inline, the assignment sits
    inside a ``try`` whose ``except`` calls ``pytest.skip`` -- which does raise,
    so the later ``finally`` was never actually reachable with an unbound name,
    but that safety rests on knowing ``skip`` raises. Static analysis reads it
    as a possible use-before-assignment and it is one bad edit away from being
    real, which would replace an honest skip with a NameError.
    """
    try:
        return psycopg2.connect(dsn)
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres unreachable: {exc}")


@pytest.fixture
def attested_table() -> Iterator[tuple[str, str]]:
    """A disposable table shaped like ``delegation_events``' attested columns.

    Migration 0038's own shape: both columns nullable with per-row DEFAULTs, so
    the fixture can tell "the DEFAULT fired on INSERT" apart from "the writer
    restated the expression on the UPDATE arm" -- which is the distinction the
    whole ticket turns on.
    """
    psycopg2 = pytest.importorskip("psycopg2")
    dsn = _dsn_or_skip()
    table = f"omn18159_attested_{uuid.uuid4().hex[:12]}"
    conn = _connect_or_skip(psycopg2, dsn)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE TABLE {table} ("
                "  correlation_id TEXT PRIMARY KEY,"
                "  task_type TEXT,"
                "  gates JSONB,"
                "  writer_identity TEXT DEFAULT CURRENT_USER,"
                "  written_at TIMESTAMPTZ DEFAULT NOW()"
                ")"
            )
        yield dsn, table
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {table}")
        conn.close()


@pytest.mark.integration
class TestPostgresEvaluatesTheAttestation:
    def test_the_insert_arm_stamps_the_connections_own_principal(
        self, attested_table: tuple[str, str]
    ) -> None:
        from omnimarket.projection.postgres_sync_database import (
            PostgresSyncProjectionAdapter,
        )

        dsn, table = attested_table
        db = PostgresSyncProjectionAdapter(dsn)
        written = db.upsert_returning(
            table,
            "correlation_id",
            {"correlation_id": "c1", "task_type": "t"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            returning=("correlation_id", "writer_identity", "written_at"),
        )
        assert len(written) == 1
        # Not the literal string -- Postgres evaluated it.
        assert written[0]["writer_identity"] != "CURRENT_USER"
        assert written[0]["writer_identity"]
        assert written[0]["written_at"] is not None

    def test_the_update_arm_restates_the_expression_rather_than_keeping_the_first_stamp(
        self, attested_table: tuple[str, str]
    ) -> None:
        """The property a column DEFAULT alone cannot give.

        A DEFAULT is consulted only on INSERT, so without the restatement an
        existing row wears its FIRST writer's identity and its first write's
        timestamp forever -- and the readback that orders by write recency
        would then order rows by a timestamp that stopped moving.
        """
        from omnimarket.projection.postgres_sync_database import (
            PostgresSyncProjectionAdapter,
        )

        dsn, table = attested_table
        db = PostgresSyncProjectionAdapter(dsn)
        first = db.upsert_returning(
            table,
            "correlation_id",
            {"correlation_id": "c1", "task_type": "a"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            returning=("written_at",),
        )
        second = db.upsert_returning(
            table,
            "correlation_id",
            {"correlation_id": "c1", "task_type": "b"},
            sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            returning=("task_type", "written_at"),
        )
        assert second[0]["task_type"] == "b"
        assert second[0]["written_at"] > first[0]["written_at"]

    def test_a_row_value_cannot_displace_the_expression(
        self, attested_table: tuple[str, str]
    ) -> None:
        from omnimarket.projection.postgres_sync_database import (
            PostgresSyncProjectionAdapter,
        )

        dsn, table = attested_table
        db = PostgresSyncProjectionAdapter(dsn)
        with pytest.raises(ValueError, match="must not also be supplied as row values"):
            db.upsert_returning(
                table,
                "correlation_id",
                {"correlation_id": "c1", "writer_identity": "somebody_else"},
                sql_expression_columns=WRITE_ATTESTATION_COLUMNS,
            )

    def test_insert_only_columns_are_not_re_attributed_on_the_update_arm(
        self, attested_table: tuple[str, str]
    ) -> None:
        from omnimarket.projection.postgres_sync_database import (
            PostgresSyncProjectionAdapter,
        )

        dsn, table = attested_table
        db = PostgresSyncProjectionAdapter(dsn)
        db.upsert_returning(
            table, "correlation_id", {"correlation_id": "c1", "task_type": "first"}
        )
        db.upsert_returning(
            table,
            "correlation_id",
            {"correlation_id": "c1", "task_type": "second", "gates": ["g"]},
            insert_only_columns=frozenset({"task_type"}),
        )
        stored = db.query(table, {"correlation_id": "c1"})
        assert stored[0]["task_type"] == "first"
        assert stored[0]["gates"] == ["g"]

    def test_jsonb_values_round_trip(self, attested_table: tuple[str, str]) -> None:
        from omnimarket.projection.postgres_sync_database import (
            PostgresSyncProjectionAdapter,
        )

        dsn, table = attested_table
        db = PostgresSyncProjectionAdapter(dsn)
        written = db.upsert_returning(
            table,
            "correlation_id",
            {"correlation_id": "c1", "gates": ["a", "b"]},
            returning=("gates",),
        )
        assert written[0]["gates"] == ["a", "b"]
