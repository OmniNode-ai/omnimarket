# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that OMN-19013 excludes only its exact quality pair."""

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
def projected_metrics() -> Iterator[Any]:
    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2.extras import RealDictCursor

    schema = f"omn19013_metrics_{uuid.uuid4().hex[:12]}"
    try:
        conn = psycopg2.connect(_dsn())
    except Exception as exc:  # pragma: no cover - environment dependent
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

            def insert(
                correlation_id: str,
                gate: bool,
                outcome: str | None = None,
                verdict: str | None = None,
            ) -> None:
                cur.execute(
                    "INSERT INTO delegation_events "
                    "(correlation_id, tenant_id, task_type, delegated_to, model_name, "
                    "quality_gate_passed, operational_outcome, content_verdict, "
                    "timestamp, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())",
                    (
                        correlation_id,
                        _TENANT,
                        "code_review",
                        "local",
                        "qwen",
                        gate,
                        outcome,
                        verdict,
                    ),
                )

            # Ordinary and legacy NULL-pair rows are positive controls. The
            # construction terminals carry their actual true/false gate facts.
            insert("ordinary-passed", True)
            insert("ordinary-failed", False)
            insert("legacy-null-pair", True)
            insert(
                "construction-passed",
                True,
                "terminal_construction_failed",
                "undetermined",
            )
            insert(
                "construction-failed",
                False,
                "terminal_construction_failed",
                "undetermined",
            )

        class _Reader:
            def row(self, view: str) -> dict[str, Any]:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    cur.execute(
                        f"SELECT * FROM {view} WHERE tenant_id = %s", (_TENANT,)
                    )
                    result = cur.fetchone()
                    assert result is not None
                    return dict(result)

            def construction_gate_facts(self) -> list[bool]:
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}, public")
                    cur.execute(
                        "SELECT quality_gate_passed FROM delegation_events "
                        "WHERE operational_outcome = 'terminal_construction_failed' "
                        "AND content_verdict = 'undetermined' "
                        "ORDER BY correlation_id"
                    )
                    return [value[0] for value in cur.fetchall()]

        yield _Reader()
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()


@pytest.mark.integration
def test_exact_terminal_pair_is_excluded_from_real_quality_readers(
    projected_metrics: Any,
) -> None:
    """All named quality readers use the applied view definitions, not a copy.

    The two construction rows must remain stored as true and false but leave
    ordinary true/false and legacy NULL-pair quality counts unchanged.
    """
    assert projected_metrics.construction_gate_facts() == [False, True]

    summary = projected_metrics.row("projection_delegation_summary")
    assert summary["totalDelegations"] == 5
    assert summary["qualityGatePassed"] == 2
    assert summary["qualityGateTotal"] == 3
    assert summary["qualityGatePassRate"] == pytest.approx(2 / 3)

    quality_gate = projected_metrics.row("projection_delegation_quality_gate")
    assert quality_gate["total_passed"] == 2
    assert quality_gate["total_failed"] == 1
    assert quality_gate["total_checks"] == 3
    assert quality_gate["overall_pass_rate"] == pytest.approx(2 / 3)

    model_routing = projected_metrics.row("projection_delegation_model_routing")
    model = model_routing["by_model"][0]
    assert model["total_count"] == 5
    assert model["qg_pass_rate"] == pytest.approx(2 / 3)
