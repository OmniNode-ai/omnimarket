# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that the model-routing view lists each tier's models (OMN-21015 AC2).

Jonah's ruling on OMN-21015 (2026-10-14T04:50Z): the Tier mix's cost per model comes from the view, summed per tier
and model, with an unmeasured count beside it. Migration 0060 adds one key, ``models``, to each tier entry.

Failure modes, each with a test below:
F1  two models in one tier serve as one entry (the ticket's falsifier);
F2  a model's share is not on the tier's denominator, so a tier's model shares do not sum to the tier's share;
F3  a run with no cost is summed as 0, so a group with no measured cost serves 0 instead of null, or the unmeasured
    count stays 0. delegation_events.cost_usd is NOT NULL DEFAULT 0 since migration 0007 until OMN-20717 makes it
    nullable, so the test drops the constraint in its own schema, as that change does, and stores null costs;
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

# (tenant, tier, model, cost) per run; None is a run with no cost (F3, stored only with the constraint dropped).
_RUNS: list[tuple[str, str, str, str | None]] = [
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
_UNPRICED_RUNS: list[tuple[str, str, str, str | None]] = [
    (_TENANT, "local", "never-priced", None),
    (_TENANT, "local", "never-priced", None),
    (_TENANT, "local", "partly-priced", "0.002"),
    (_TENANT, "local", "partly-priced", None),
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


def _routing(
    runs: list[tuple[str, str, str, str | None]], *, nullable_cost: bool
) -> Iterator[Any]:
    psycopg2 = pytest.importorskip("psycopg2")
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
            if nullable_cost:
                cur.execute(
                    "ALTER TABLE delegation_events ALTER COLUMN cost_usd DROP NOT NULL, "
                    "ALTER COLUMN cost_usd DROP DEFAULT"
                )
            for tenant, tier, model, cost in runs:
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


@pytest.fixture(scope="module")
def routing() -> Iterator[Any]:
    yield from _routing(_RUNS, nullable_cost=False)


@pytest.fixture(scope="module")
def unpriced_routing() -> Iterator[Any]:
    yield from _routing(_UNPRICED_RUNS, nullable_cost=True)


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
def test_f3_a_run_with_no_cost_is_unmeasured_not_zero(unpriced_routing: Any) -> None:
    local = {
        m["model_name"]: m for m in unpriced_routing.tiers(_TENANT)["local"]["models"]
    }
    assert local["never-priced"]["cost_usd"] is None
    assert local["never-priced"]["unmeasured_cost_count"] == 2
    assert float(local["partly-priced"]["cost_usd"]) == pytest.approx(0.002)
    assert local["partly-priced"]["unmeasured_cost_count"] == 1


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
