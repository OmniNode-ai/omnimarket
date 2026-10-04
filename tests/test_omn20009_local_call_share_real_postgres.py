# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof of the served Run-locally share (OMN-20009).

The Overview's "Run locally" figure is the share of ALL runs whose tier is
``local``, not-tier-routed runs included in the denominator. Before OMN-20009
the view served only ``pct_of_tier_routed``, whose denominator leaves the
not-tier-routed runs out: on the lab on 2026-10-04 it read 1.0 (100 %) for 104
local runs out of 124, where the share is 104/124. The dashboard may not
divide, so the view serves the counts and the share itself.

Failure modes, each with a test below:

F1  a mix of local and not-tier-routed runs: the share is local over all runs,
    never local over tier-routed runs;
F2  another tier (a cloud tier) counts in the denominator, not the numerator;
F3  a tenant with runs but none local serves a measured 0 share;
F4  a tenant with no runs serves no row, so the page shows its empty state;
F5  another tenant's runs never enter this tenant's counts;
F6  the keys the dashboard already reads (``tiers``, ``pct_of_tier_routed``,
    ``total_tasks``, ``not_tier_routed_count``) are unchanged;
F7  the replaced view keeps ``security_invoker`` (row-level security).
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
_MIXED = "11111111-1111-4111-8111-111111111111"
_CLOUD_ONLY = "22222222-2222-4222-8222-222222222222"
_EMPTY = "33333333-3333-4333-8333-333333333333"
_VIEW = "projection_delegation_model_routing"


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
    from psycopg2.extras import RealDictCursor

    schema = f"omn20009_share_{uuid.uuid4().hex[:12]}"
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
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}, public")
            for path in sorted(_MIGRATIONS.glob("*.sql")):
                if path.name != _FENCED:
                    cur.execute(_schema_safe(path.read_text(encoding="utf-8")))

            def insert(tenant: str, tier: str, count: int) -> None:
                for _ in range(count):
                    cur.execute(
                        "INSERT INTO delegation_events "
                        "(correlation_id, tenant_id, task_type, delegated_to, "
                        "model_name, cost_tier_name, quality_gate_passed, "
                        "timestamp, created_at) "
                        "VALUES (%s, %s, 'code_review', 'local', 'qwen', %s, TRUE, "
                        "NOW(), NOW())",
                        (
                            f"omn20009-{uuid.uuid4().hex}",
                            tenant,
                            tier,
                        ),
                    )

            # The lab's shape on 2026-10-04 scaled down: 6 local, 2 not tier
            # routed (empty tier), plus 2 on a cloud tier.
            insert(_MIXED, "local", 6)
            insert(_MIXED, "", 2)
            insert(_MIXED, "cheap_cloud", 2)
            insert(_CLOUD_ONLY, "cheap_cloud", 3)

        class _Reader:
            def by_tier(self, tenant: str) -> dict[str, Any] | None:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    cur.execute(
                        f"SELECT by_tier FROM {_VIEW} WHERE tenant_id = %s", (tenant,)
                    )
                    rows = cur.fetchall()
                    assert len(rows) <= 1
                    return dict(rows[0]["by_tier"]) if rows else None

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
def test_f1_f2_share_is_local_over_all_runs(routing: Any) -> None:
    by_tier = routing.by_tier(_MIXED)
    assert by_tier is not None
    assert by_tier["local_call_count"] == 6
    assert by_tier["total_call_count"] == 10
    assert by_tier["local_call_share"] == pytest.approx(6 / 10)
    # The tier-routed share the dashboard must not show as "run locally".
    local = next(t for t in by_tier["tiers"] if t["cost_tier_name"] == "local")
    assert local["pct_of_tier_routed"] == pytest.approx(6 / 8)
    assert by_tier["local_call_share"] != pytest.approx(local["pct_of_tier_routed"])


@pytest.mark.integration
def test_f3_runs_with_none_local_serve_a_measured_zero(routing: Any) -> None:
    by_tier = routing.by_tier(_CLOUD_ONLY)
    assert by_tier is not None
    assert by_tier["local_call_count"] == 0
    assert by_tier["total_call_count"] == 3
    assert by_tier["local_call_share"] == 0


@pytest.mark.integration
def test_f4_a_tenant_with_no_runs_serves_no_row(routing: Any) -> None:
    assert routing.by_tier(_EMPTY) is None


@pytest.mark.integration
def test_f5_another_tenants_runs_never_enter_the_counts(routing: Any) -> None:
    mixed = routing.by_tier(_MIXED)
    cloud = routing.by_tier(_CLOUD_ONLY)
    assert mixed is not None
    assert cloud is not None
    assert mixed["total_call_count"] + cloud["total_call_count"] == 13
    assert mixed["total_call_count"] == mixed["total_tasks"]


@pytest.mark.integration
def test_f6_existing_by_tier_keys_are_unchanged(routing: Any) -> None:
    by_tier = routing.by_tier(_MIXED)
    assert by_tier is not None
    assert by_tier["total_tasks"] == 10
    assert by_tier["tier_routed_total"] == 8
    assert by_tier["not_tier_routed_count"] == 2
    names = [t["cost_tier_name"] for t in by_tier["tiers"]]
    assert names == ["local", "cheap_cloud", "not_tier_routed"]


@pytest.mark.integration
def test_f7_replaced_view_keeps_security_invoker(routing: Any) -> None:
    assert routing.invoker() is True
