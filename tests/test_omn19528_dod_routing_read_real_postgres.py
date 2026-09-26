# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the DoD routing read's join (OMN-19528, AC2).

The unit module proves the fold and the reader's statement shape against a fake
connection. This runs the reader's own SQL against a real server: the verdict
table is built from the node's real migrations (0000 and 0002), and the
delegation side is a stand-in with the lab's column types (``correlation_id``
TEXT, ``tenant_id`` UUID), so the UUID-to-TEXT cast in the join is exercised
exactly as it runs on the lab.

It asserts the three things only a real server can show: the cast joins, the
tenant filter excludes another tenant's run, and an unlinked verdict joins to
nothing.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import pytest

from omnimarket.routing.dod_overlay import PostgresDodOutcomeReader, build_dod_overlay

pytestmark = pytest.mark.integration

_MIGRATIONS = Path(__file__).resolve().parents[1] / (
    "src/omnimarket/nodes/node_projection_dod_verdict/migrations"
)
MIGRATIONS = (
    _MIGRATIONS / "0000_create_dod_verify_runs.sql",
    _MIGRATIONS / "0002_dod_verify_runs_delegation_correlation_id.sql",
)

TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
OTHER_TENANT = "16976000-0000-4000-8000-0000000000aa"
BASE = datetime(2026, 9, 25, 3, 0, tzinfo=UTC)


def _dsn() -> str:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"


@pytest.fixture
def schema() -> Iterator[str]:
    psycopg2 = pytest.importorskip("psycopg2")
    if not os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    ):
        pytest.skip("POSTGRES_PASSWORD not set -- skipping the OMN-19528 join test")
    try:
        conn = psycopg2.connect(_dsn(), connect_timeout=3)
    except psycopg2.OperationalError as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the OMN-19528 join test: {exc}")
    conn.autocommit = True
    name = f"omn19528_{uuid4().hex[:12]}"
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {name}")
            for migration in MIGRATIONS:
                cur.execute(
                    migration.read_text().replace("omninode_internal.", f"{name}.")
                )
            cur.execute(
                f"CREATE TABLE {name}.delegation_events ("
                "correlation_id TEXT NOT NULL, task_type TEXT NOT NULL, "
                "cost_tier_name TEXT, model_name TEXT, tenant_id UUID NOT NULL, "
                "created_at TIMESTAMPTZ NOT NULL)"
            )
        yield name
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {name} CASCADE")
        conn.close()


def _insert(
    schema: str,
    *,
    tenant: str,
    task_type: str,
    tier: str,
    model: str,
    status: str | None,
    outcome: str = "refused",
    linked: bool = True,
) -> str:
    import psycopg2  # type: ignore[import-untyped]

    run = str(uuid4())
    conn = psycopg2.connect(_dsn(), connect_timeout=3)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {schema}.delegation_events VALUES (%s,%s,%s,%s,%s,%s)",
                (run, task_type, tier, model, tenant, BASE),
            )
            if status is not None:
                cur.execute(
                    f"INSERT INTO {schema}.dod_verify_runs (ticket_id, correlation_id, "
                    "completed_at, started_at, status, total_checks, verified_count, "
                    "failed_count, skipped_count, superseded_count, non_probative_count, "
                    "behavior_proving_count, outcome, delegation_correlation_id, projected_at) "
                    "VALUES ('OMN-19528', %s, %s, %s, %s, 1, 1, 0, 0, 0, 0, 1, %s, %s, now())",
                    (
                        str(uuid4()),
                        BASE + timedelta(minutes=5),
                        BASE,
                        status,
                        outcome,
                        run if linked else None,
                    ),
                )
    finally:
        conn.close()
    return run


@pytest.mark.integration
def test_the_join_casts_filters_the_tenant_and_folds_a_rate(schema: str) -> None:
    passed = _insert(
        schema,
        tenant=TENANT,
        task_type="document",
        tier="local",
        model="Qwen3.8-27B",
        status="verified",
        outcome="done",
    )
    failed = _insert(
        schema,
        tenant=TENANT,
        task_type="document",
        tier="local",
        model="Qwen3.8-27B",
        status="failed",
    )
    # Controls that must NOT come back: another tenant, another task type, an
    # unlinked verdict, and a run with no verdict at all.
    _insert(
        schema,
        tenant=OTHER_TENANT,
        task_type="document",
        tier="local",
        model="Qwen3.8-27B",
        status="verified",
        outcome="done",
    )
    _insert(
        schema,
        tenant=TENANT,
        task_type="test",
        tier="local",
        model="Qwen3.8-27B",
        status="verified",
        outcome="done",
    )
    _insert(
        schema,
        tenant=TENANT,
        task_type="document",
        tier="local",
        model="Qwen3.8-27B",
        status="verified",
        outcome="done",
        linked=False,
    )
    _insert(
        schema,
        tenant=TENANT,
        task_type="document",
        tier="local",
        model="Qwen3.8-27B",
        status=None,
    )

    reader = PostgresDodOutcomeReader(
        _dsn(),
        _dsn(),
        delegation_relation=f"{schema}.delegation_events",
        verdict_relation=f"{schema}.dod_verify_runs",
    )
    try:
        rows = reader.read_dod_outcomes(task_type="document", tenant_id=TENANT)
    finally:
        reader.close()

    assert sorted(str(r["correlation_id"]) for r in rows) == sorted([passed, failed])
    overlay = build_dod_overlay(
        rows,
        task_type="document",
        tenant_id=TENANT,
        min_samples=5,
        success_floor=0.5,
        lookback_rows=None,
        window_seconds=None,
    )
    (signal,) = overlay.model_signals
    assert (signal.tier_name, signal.model_name) == ("local", "Qwen3.8-27B")
    assert (signal.sample_count, signal.pass_count, signal.pass_rate) == (2, 1, 0.5)
