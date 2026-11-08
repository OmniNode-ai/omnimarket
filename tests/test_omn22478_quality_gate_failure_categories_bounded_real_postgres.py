# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22573: real-Postgres proof that the quality-gate failure list is bounded.

The delegation projection writer republishes ``projection_delegation_quality_gate``
for a tenant after every apply. Migration 0045 grouped ``failure_categories`` on
the whole free-text ``quality_gate_detail``, so one dev tenant reached 2,586
distinct values and 1,166,913 bytes, over the 1,048,576-byte snapshot limit, and
the writer quarantined every event it read. Migration 0065 bounds the list.

The migrations are applied to a throwaway schema. Without a reachable Postgres
the module skips with the variable to set. Run against the view of migration
0045 (migration 0065 absent) these tests fail, which is the control.
"""

from __future__ import annotations

import json
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
_VIEW = "projection_delegation_quality_gate"
_OTHER = "other failure categories"
#: One bus message is limited to 1,048,576 bytes (KAFKA_MAX_REQUEST_SIZE); the
#: whole aggregate must sit far below it, so the list gets a small share.
_LIST_BYTES_BUDGET = 20_000


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


@pytest.fixture
def schema_cursor() -> Iterator[Any]:
    psycopg2 = pytest.importorskip("psycopg2")

    schema = f"omn22573_{uuid.uuid4().hex[:12]}"
    dsn = _dsn()
    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable: {exc}")
        raise
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
            yield cur
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


def _insert_failed(cur: Any, label: str, detail: str | None, *, passed: bool) -> None:
    cur.execute(
        "INSERT INTO delegation_events "
        "(correlation_id, tenant_id, task_type, delegated_to, model_name, "
        "quality_gate_passed, quality_gate_detail, timestamp, created_at) "
        "VALUES (%s, %s, 'code_review', 'local', 'qwen', %s, %s, NOW(), NOW())",
        (label, _TENANT, passed, detail),
    )


def _row(cur: Any) -> tuple[list[dict[str, Any]], int, int]:
    cur.execute(
        f"SELECT failure_categories::text, total_failed, "
        f"octet_length(failure_categories::text) FROM {_VIEW} WHERE tenant_id = %s",
        (_TENANT,),
    )
    categories, total_failed, size = cur.fetchone()
    return json.loads(categories), total_failed, size


def test_three_hundred_distinct_details_give_a_bounded_list(schema_cursor: Any) -> None:
    cur = schema_cursor
    # 300 families, each with a long run-specific tail beyond the 120 characters
    # that name a category; family k has k + 1 rows so the order is fixed.
    for family in range(300):
        for run in range(family + 1):
            detail = f"family-{family:03d} " + "x" * 150 + f" run-{run}"
            _insert_failed(cur, f"f{family}-{run}", detail, passed=False)
    categories, total_failed, size = _row(cur)

    assert len(categories) == 51
    assert categories[-1]["category"] == _OTHER
    assert all(len(entry["category"]) <= 120 for entry in categories[:-1])
    assert sum(entry["count"] for entry in categories) == total_failed
    assert size < _LIST_BYTES_BUDGET
    # The largest family leads, as in the view of migration 0045.
    assert categories[0]["category"].startswith("family-299 ")
    assert categories[0]["count"] == 300


def test_details_that_differ_only_after_the_category_text_are_one_family(
    schema_cursor: Any,
) -> None:
    cur = schema_cursor
    for run in range(40):
        _insert_failed(
            cur,
            f"same-{run}",
            "abandoned in state ROUTED " + "y" * 120 + f" {run}",
            passed=False,
        )
    categories, total_failed, _ = _row(cur)

    assert len(categories) == 1
    assert categories[0]["count"] == 40 == total_failed
    assert categories[0]["pct_of_failures"] == pytest.approx(1.0)


def test_failed_rows_without_a_detail_stay_in_the_denominator_only(
    schema_cursor: Any,
) -> None:
    cur = schema_cursor
    for run in range(3):
        _insert_failed(cur, f"d{run}", "below_bar", passed=False)
    _insert_failed(cur, "nodetail", None, passed=False)
    _insert_failed(cur, "passed", "ignored", passed=True)
    categories, total_failed, _ = _row(cur)

    assert total_failed == 4
    assert categories == [
        {"category": "below_bar", "count": 3, "pct_of_failures": 0.75}
    ]


def test_the_view_keeps_the_invoker_rights_the_tenant_rls_depends_on(
    schema_cursor: Any,
) -> None:
    cur = schema_cursor
    cur.execute(
        "SELECT 'security_invoker=true' = ANY(c.reloptions) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE c.relname = %s AND n.nspname = current_schema()",
        (_VIEW,),
    )
    assert cur.fetchone()[0] is True
