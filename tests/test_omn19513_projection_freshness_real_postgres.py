# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19513: the freshness reader's SQL, run against real Postgres.

The in-memory tests answer the reader with a stand-in, so the two aggregates are
never executed there. Only a real driver proves that ``max(<timestamptz>)``
comes back as an aware datetime, that the bound demand cutoff is accepted as a
``TIMESTAMPTZ`` parameter, and that ``count(DISTINCT ...)`` is an integer. The
tables are disposable copies of the two relations' read columns in a scratch
schema; the check names them through the same contract-shaped model production
uses.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg2
import pytest

from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.classify_freshness import (
    classify_freshness,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.handlers.freshness_reader import (
    PostgresFreshnessReader,
)
from omnimarket.nodes.node_projection_freshness_alert_effect.models import (
    EnumFreshnessState,
    ModelFreshnessCheck,
)

_SCHEMA = "omn19513_freshness_read_test"
_NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)
_CHECK = ModelFreshnessCheck.model_validate(
    {
        "check_id": "scratch_supply",
        "max_age_seconds": 900,
        "supply": {
            "relation": f"{_SCHEMA}.supply_rows",
            "timestamp_column": "emitted_at",
        },
        "demand": {
            "relation": f"{_SCHEMA}.demand_rows",
            "timestamp_column": "emitted_at",
            "distinct_column": "actor_id",
            "window_seconds": 900,
            "min_distinct": 1,
        },
    }
)


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- no real-DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"host={host} port={port} user={user} password={password} dbname={db}"


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    dsn = _dsn()
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.skip(f"no reachable Postgres for the freshness proof: {exc}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_SCHEMA}")
        cur.execute(f"CREATE TABLE {_SCHEMA}.supply_rows (emitted_at TIMESTAMPTZ)")
        cur.execute(
            f"CREATE TABLE {_SCHEMA}.demand_rows "
            "(emitted_at TIMESTAMPTZ NOT NULL, actor_id TEXT NOT NULL)"
        )
    try:
        yield dsn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        conn.close()


def _seed(dsn: str, *, supply_age: int | None, actors: dict[str, int]) -> None:
    conn = psycopg2.connect(dsn, connect_timeout=5)
    conn.autocommit = True
    with conn.cursor() as cur:
        if supply_age is not None:
            cur.execute(
                f"INSERT INTO {_SCHEMA}.supply_rows VALUES (%s)",
                (_NOW - timedelta(seconds=supply_age),),
            )
        for actor, age in actors.items():
            cur.execute(
                f"INSERT INTO {_SCHEMA}.demand_rows VALUES (%s, %s)",
                (_NOW - timedelta(seconds=age), actor),
            )
    conn.close()


def _classify(dsn: str) -> EnumFreshnessState:
    reader = PostgresFreshnessReader(dsn=dsn, now=lambda: _NOW)
    try:
        observation = reader.read(_CHECK)
    finally:
        reader.close()
    return classify_freshness(
        check_id=_CHECK.check_id,
        now=_NOW,
        supply_newest_at=observation.supply_newest_at,
        demand_distinct_active=observation.demand_distinct_active,
        max_age_seconds=_CHECK.max_age_seconds,
        min_distinct=_CHECK.demand.min_distinct,
    ).state


@pytest.mark.integration
def test_old_supply_with_active_demand_reads_back_stale(scratch_dsn: str) -> None:
    _seed(scratch_dsn, supply_age=7920, actors={"a": 60, "b": 120, "c": 3000})
    reader = PostgresFreshnessReader(dsn=scratch_dsn, now=lambda: _NOW)
    observation = reader.read(_CHECK)
    reader.close()
    assert observation.supply_newest_at == _NOW - timedelta(seconds=7920)
    assert observation.demand_distinct_active == 2
    assert _classify(scratch_dsn) is EnumFreshnessState.STALE


@pytest.mark.integration
def test_recent_supply_reads_back_fresh(scratch_dsn: str) -> None:
    _seed(scratch_dsn, supply_age=30, actors={"a": 60})
    assert _classify(scratch_dsn) is EnumFreshnessState.FRESH


@pytest.mark.integration
def test_old_supply_with_only_old_demand_reads_back_idle(scratch_dsn: str) -> None:
    _seed(scratch_dsn, supply_age=7920, actors={"a": 5000})
    assert _classify(scratch_dsn) is EnumFreshnessState.IDLE


@pytest.mark.integration
def test_an_empty_supply_table_with_active_demand_is_stale(scratch_dsn: str) -> None:
    _seed(scratch_dsn, supply_age=None, actors={"a": 60})
    assert _classify(scratch_dsn) is EnumFreshnessState.STALE
