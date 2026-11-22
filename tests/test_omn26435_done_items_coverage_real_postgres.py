# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26435: the coverage reader's SQL and parsing, run against real Postgres.

The in-memory tests answer the reader with a stand-in, so the coverage query is
never executed there. Only a real driver proves that the bound demand cutoff is
accepted as a ``TIMESTAMPTZ`` parameter, that the ledger rows parse into the
expected terminal tickets, and that the projected supply rows are matched back
to their ledger rows by ``done_row_id``. The tables are disposable copies of the
two relations' read columns in a scratch schema; the check names them through
the same contract-shaped model production uses.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest

from omnimarket.handlers.ledger_row_parser import row_id_of
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.classify_done_items_coverage import (
    classify_done_items_coverage,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.freshness_reader import (
    PostgresFreshnessReader,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
    ModelCoverageCheck,
    ModelCoverageObservation,
)

_SCHEMA = "omn26435_coverage_read_test"
_NOW = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
_CHECK = ModelCoverageCheck.model_validate(
    {
        "check_id": "done_items_coverage",
        "demand_relation": f"{_SCHEMA}.work_ledger_rows",
        "supply_relation": f"{_SCHEMA}.done_items",
        "max_age_seconds": 1800,
        "lookback_seconds": 86400,
    }
)


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        if os.environ.get("OMN_REQUIRE_REAL_DB") == "1":
            pytest.fail("INTEGRATION_POSTGRES_PASSWORD not set -- no real-DB proof")
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- no real-DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"host={host} port={port} user={user} password={password} dbname={db}"


def _connect_or_skip(dsn: str) -> psycopg2.extensions.connection:
    try:
        return psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        if os.environ.get("OMN_REQUIRE_REAL_DB") == "1":
            pytest.fail(f"no reachable Postgres for the coverage proof: {exc}")
        pytest.skip(f"no reachable Postgres for the coverage proof: {exc}")


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    dsn = _dsn()
    conn = _connect_or_skip(dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCHEMA}")
        cur.execute(
            f"CREATE TABLE {_SCHEMA}.work_ledger_rows ("
            "row_id TEXT PRIMARY KEY, "
            "row_ts TIMESTAMPTZ NOT NULL, "
            "row_type TEXT NOT NULL, "
            "raw_row TEXT NOT NULL)"
        )
        cur.execute(
            f"CREATE TABLE {_SCHEMA}.done_items ("
            "ticket TEXT PRIMARY KEY, "
            "done_at TEXT NOT NULL, "
            "done_row_id TEXT NOT NULL)"
        )
    try:
        yield dsn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()


def _stamp(age: int) -> str:
    return (_NOW - timedelta(seconds=age)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _raw_row(ticket: str, outcome: str, age: int) -> str:
    stamp = _stamp(age)
    return f"{stamp} | TERMINAL | lane=alpha | ticket={ticket} | outcome={outcome} | finished"


def _seed_ledger_row(dsn: str, *, ticket: str, outcome: str, age: int) -> str:
    raw = _raw_row(ticket, outcome, age)
    rid = row_id_of(raw)
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {_SCHEMA}.work_ledger_rows (row_id, row_ts, row_type, raw_row) "
            "VALUES (%s, %s, %s, %s)",
            (rid, _NOW - timedelta(seconds=age), "TERMINAL", raw),
        )
    conn.close()
    return rid


def _seed_status_row(dsn: str, ticket: str, age: int) -> str:
    stamp = _stamp(age)
    raw = f"{stamp} | STATUS | lane=alpha | ticket={ticket} | merged=1"
    rid = row_id_of(raw)
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {_SCHEMA}.work_ledger_rows (row_id, row_ts, row_type, raw_row) "
            "VALUES (%s, %s, %s, %s)",
            (rid, _NOW - timedelta(seconds=age), "STATUS", raw),
        )
    conn.close()
    return rid


def _seed_done_item(dsn: str, *, ticket: str, done_row_id: str, age: int) -> None:
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {_SCHEMA}.done_items (ticket, done_at, done_row_id) "
            "VALUES (%s, %s, %s)",
            (ticket, _stamp(age), done_row_id),
        )
    conn.close()


def _read_coverage(dsn: str) -> ModelCoverageObservation:
    reader = PostgresFreshnessReader(dsn=dsn, now=lambda: _NOW)
    try:
        return reader.read_coverage(_CHECK)
    finally:
        reader.close()


def _classify(dsn: str) -> EnumFreshnessState:
    observation = _read_coverage(dsn)
    return classify_done_items_coverage(
        check_id=_CHECK.check_id,
        demand_done_tickets=observation.demand_done_tickets,
        unprojected_tickets=observation.unprojected_tickets,
        refused_rows=observation.refused_rows,
    ).state


@pytest.mark.integration
def test_unprojected_old_done_event_reads_back_stale(scratch_dsn: str) -> None:
    _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="done", age=3600)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 1
    assert _classify(scratch_dsn) is EnumFreshnessState.STALE


@pytest.mark.integration
def test_projected_done_event_reads_back_fresh(scratch_dsn: str) -> None:
    rid = _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="done", age=3600)
    _seed_done_item(scratch_dsn, ticket="OMN-1", done_row_id=rid, age=3600)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 0
    assert _classify(scratch_dsn) is EnumFreshnessState.FRESH


@pytest.mark.integration
def test_handed_off_only_reads_back_idle(scratch_dsn: str) -> None:
    _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="handed-off", age=3600)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 0
    assert observation.unprojected_tickets == 0
    assert _classify(scratch_dsn) is EnumFreshnessState.IDLE


@pytest.mark.integration
def test_recent_done_event_is_not_yet_demand(scratch_dsn: str) -> None:
    _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="done", age=600)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 0
    assert observation.unprojected_tickets == 0
    assert _classify(scratch_dsn) is EnumFreshnessState.IDLE


@pytest.mark.integration
def test_stale_supply_row_counts_as_unprojected(scratch_dsn: str) -> None:
    rid_old = _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="done", age=7200)
    _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="done", age=3600)
    _seed_done_item(scratch_dsn, ticket="OMN-1", done_row_id=rid_old, age=7200)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 1
    assert observation.unprojected_tickets == 1
    assert _classify(scratch_dsn) is EnumFreshnessState.STALE


@pytest.mark.integration
def test_done_event_older_than_the_lookback_is_not_demand(scratch_dsn: str) -> None:
    _seed_ledger_row(scratch_dsn, ticket="OMN-1", outcome="done", age=90000)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 0
    assert observation.unprojected_tickets == 0
    assert _classify(scratch_dsn) is EnumFreshnessState.IDLE


@pytest.mark.integration
def test_status_rows_are_not_demand(scratch_dsn: str) -> None:
    _seed_status_row(scratch_dsn, "OMN-1", 3600)
    observation = _read_coverage(scratch_dsn)
    assert observation.demand_done_tickets == 0
    assert observation.unprojected_tickets == 0
    assert _classify(scratch_dsn) is EnumFreshnessState.IDLE
