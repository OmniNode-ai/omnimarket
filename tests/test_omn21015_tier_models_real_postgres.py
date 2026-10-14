# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that the model-routing view lists each tier's models (OMN-21015 AC2).

Jonah's ruling on OMN-21015 (2026-10-14T04:50Z): the Tier mix's cost per model comes from the view, summed per tier
and model, with an unmeasured count beside it. Migration 0060 adds one key, ``models``, to each tier entry.

Failure modes, each with a test below:
F1  two models in one tier serve as one entry (the ticket's falsifier);
F2  a model's share is not on the tier's denominator, so a tier's model shares do not sum to the tier's share;
F3  on Postgres an unpriced run cannot be stored as such: delegation_events.cost_usd is NOT NULL DEFAULT 0
    (migration 0007), so the view's unmeasured count is 0 there and a null cost exists only on the local store.
    This test pins that limit, so making the column nullable later turns it red and is noticed;
F4  a measured cost is not summed, or a measured zero reads as unmeasured;
F5  another tenant's runs enter this tenant's models;
F6  the keys the page already reads change;
F7  the replaced view loses ``security_invoker`` (row-level security).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
    / "migrations"
)
_FENCED = "0031_delegation_events_tenant_id_to_uuid.sql"
_TENANT = "11111111-1111-4111-8111-111111111111"
_OTHER = "22222222-2222-4222-8222-222222222222"
_VIEW = "projection_delegation_model_routing"

# (tenant, tier, model, cost) per run. Postgres stores no unpriced cost (see F3), so every run carries one.
_RUNS: list[tuple[str, str, str, str]] = [
    (_TENANT, "local", "qwen-a", "0"),
    (_TENANT, "local", "qwen-a", "0"),
    (_TENANT, "local", "qwen-a", "0"),
    (_TENANT, "local", "qwen-b", "0.001"),
    (_TENANT, "cheap_cloud", "gemini", "0.004"),
    (_TENANT, "cheap_cloud", "gemini", "0.005"),
    (_TENANT, "", "unrouted-model", "0"),
    (_OTHER, "local", "qwen-a", "0"),
    (_OTHER, "local", "qwen-a", "0"),
]


def _dsn() -> str:
    password = os.environ.get("INTEGRATION_POSTGRES_PASSWORD") or os.environ.get(
        "POSTGRES_PASSWORD"
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD/POSTGRES_PASSWORD unset")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "127.0.0.1")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5436")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    database = os.environ.get("INTEGRATION_POSTGRES_DB", "omnidash_analytics")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


def _schema_safe(sql: str) -> str:
    return sql.replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")


@pytest.fixture(scope="module")
def routing() -> Iterator[Any]:
    psycopg2 = pytest.importorskip("psycopg2")
    pg_errors = pytest.importorskip("psycopg2.errors")
    from psycopg2.extras import RealDictCursor

    schema = f"omn21015_tiers_{uuid.uuid4().hex[:12]}"
    try:
        conn = psycopg2.connect(_dsn())
    except Exception as exc:
        pytest.skip(f"Postgres unreachable: {exc}")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_dashboard') "
                "THEN CREATE ROLE app_dashboard; END IF; "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles "
                "WHERE rolname = 'tenant_projection_writer') THEN "
                "CREATE ROLE tenant_projection_writer WITH NOLOGIN NOSUPERUSER "
                "NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION; END IF; "
                "END$$;"
            )
            cur.execute("CREATE SCHEMA IF NOT EXISTS omninode_internal")
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}, public")
            for path in sorted(_MIGRATIONS.glob("*.sql")):
                if path.name != _FENCED:
                    cur.execute(_schema_safe(path.read_text(encoding="utf-8")))
            for tenant, tier, model, cost in _RUNS:
                cur.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, tenant_id, task_type, delegated_to, model_name, "
                    "cost_tier_name, cost_usd, quality_gate_passed, timestamp, created_at) "
                    "VALUES (%s, %s, 'research', %s, %s, %s, %s::numeric, TRUE, NOW(), NOW())",
                    (f"omn21015-{uuid.uuid4().hex}", tenant, model, model, tier, cost),
                )

        class _Reader:
            def tiers(self, tenant: str) -> dict[str, dict[str, Any]]:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    cur.execute(
                        f"SELECT by_tier FROM {_VIEW} WHERE tenant_id = %s", (tenant,)
                    )
                    rows = cur.fetchall()
                    assert len(rows) == 1
                    return {t["cost_tier_name"]: t for t in rows[0]["by_tier"]["tiers"]}

            def null_cost_refused(self) -> bool:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    try:
                        cur.execute(
                            "INSERT INTO delegation_events (correlation_id, tenant_id, task_type, "
                            "delegated_to, cost_tier_name, cost_usd, quality_gate_passed, timestamp, "
                            "created_at) VALUES (%s, %s, 'research', 'x', 'local', NULL, TRUE, NOW(), NOW())",
                            (f"omn21015-null-{uuid.uuid4().hex}", _TENANT),
                        )
                    except pg_errors.NotNullViolation:
                        return True
                    return False

            def invoker(self) -> bool:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT 'security_invoker=true' = ANY(c.reloptions) "
                        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = %s AND c.relname = %s",
                        (schema, _VIEW),
                    )
                    return bool(cur.fetchone()[0])

        yield _Reader()
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


@pytest.mark.integration
def test_f1_two_models_in_one_tier_are_two_entries(routing: Any) -> None:
    local = routing.tiers(_TENANT)["local"]
    assert [(m["model_name"], m["count"]) for m in local["models"]] == [
        ("qwen-a", 3),
        ("qwen-b", 1),
    ]


@pytest.mark.integration
def test_f2_model_shares_sum_to_the_tier_share(routing: Any) -> None:
    tiers = routing.tiers(_TENANT)
    for name in ("local", "cheap_cloud"):
        shares = [m["pct_of_tier_routed"] for m in tiers[name]["models"]]
        assert sum(shares) == pytest.approx(tiers[name]["pct_of_tier_routed"])
    assert [m["pct_of_tier_routed"] for m in tiers["not_tier_routed"]["models"]] == [0]


@pytest.mark.integration
def test_f3_postgres_stores_no_unpriced_cost_so_none_is_counted(routing: Any) -> None:
    local = {m["model_name"]: m for m in routing.tiers(_TENANT)["local"]["models"]}
    assert all(m["unmeasured_cost_count"] == 0 for m in local.values())
    assert float(local["qwen-b"]["cost_usd"]) == pytest.approx(0.001)
    assert routing.null_cost_refused() is True


@pytest.mark.integration
def test_f4_measured_costs_sum_and_a_measured_zero_is_zero(routing: Any) -> None:
    tiers = routing.tiers(_TENANT)
    gemini = tiers["cheap_cloud"]["models"][0]
    assert float(gemini["cost_usd"]) == pytest.approx(0.009)
    assert gemini["unmeasured_cost_count"] == 0
    assert float(tiers["local"]["models"][0]["cost_usd"]) == 0
    assert tiers["local"]["models"][0]["unmeasured_cost_count"] == 0


@pytest.mark.integration
def test_f5_another_tenant_never_enters(routing: Any) -> None:
    assert routing.tiers(_TENANT)["local"]["models"][0]["count"] == 3
    assert routing.tiers(_OTHER)["local"]["models"] == [
        {
            "model_name": "qwen-a",
            "count": 2,
            "pct_of_tier_routed": 1.0,
            "cost_usd": 0,
            "unmeasured_cost_count": 0,
        }
    ]


@pytest.mark.integration
def test_f6_existing_tier_keys_unchanged(routing: Any) -> None:
    for tier in routing.tiers(_TENANT).values():
        assert {"cost_tier_name", "count", "tier_routed", "pct_of_tier_routed"} <= set(
            tier
        )


@pytest.mark.integration
def test_f7_view_keeps_security_invoker(routing: Any) -> None:
    assert routing.invoker() is True
