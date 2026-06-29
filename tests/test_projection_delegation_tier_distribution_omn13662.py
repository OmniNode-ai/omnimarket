# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-13662 (T3): explicit ``not_tier_routed`` classification in the delegation
model-routing rollup.

Context
-------
The delegate-skill terminal path (``ModelDelegateSkillTerminalProjection``) is
skill->node delegation, NOT LLM-tier routing, so those ``delegation_events`` rows
carry an empty ``cost_tier_name`` (TEXT NOT NULL DEFAULT '' from migration 0018).

Authoritative projection rule (no silent default):
  * map empty ``cost_tier_name`` -> the explicit value ``not_tier_routed``;
  * EXCLUDE ``not_tier_routed`` rows from tier-distribution percentage
    DENOMINATORS (percentages are computed over LLM-tier-routed rows only and
    therefore sum to 1.0);
  * INCLUDE ``not_tier_routed`` rows in total task counts;
  * surface the classification + the excluded count on the projection so the
    dashboard READS it and never re-derives it.

These tests pin the rule three ways:
  1. static structure of the committed view DDL + the contract wiring (always
     runs, no DB);
  2. a Python reference implementation of the denominator math over a mixed
     fixture set (always runs, no DB);
  3. real-Postgres execution of the actual view DDL against a fixture table
     (``@pytest.mark.integration`` -- skips without a live DB).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import asyncpg
import pytest
import yaml

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_delegation"
)
_MIGRATION_SQL = (
    _NODE_DIR / "migrations" / "0021_delegation_tier_distribution_not_tier_routed.sql"
)
_CONTRACT = _NODE_DIR / "contract.yaml"
_MODEL_ROUTING_TOPIC = "onex.snapshot.projection.delegation.model-routing.v1"

_NOT_TIER_ROUTED = "not_tier_routed"


# ---------------------------------------------------------------------------
# 1. Static structure: the committed view DDL + the contract wiring.
# ---------------------------------------------------------------------------


def _read_migration_sql() -> str:
    return _MIGRATION_SQL.read_text(encoding="utf-8")


def _model_routing_exposure() -> dict[str, Any]:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    for exposure in data["projection_api"]["exposures"]:
        if exposure["topic"] == _MODEL_ROUTING_TOPIC:
            return dict(exposure)
    raise AssertionError(
        f"No projection_api exposure declared for topic {_MODEL_ROUTING_TOPIC!r}"
    )


@pytest.mark.unit
def test_migration_artifact_exists() -> None:
    assert _MIGRATION_SQL.is_file(), (
        f"missing tier-distribution migration: {_MIGRATION_SQL}"
    )


@pytest.mark.unit
def test_migration_redefines_model_routing_view() -> None:
    sql = _read_migration_sql()
    assert "CREATE OR REPLACE VIEW projection_delegation_model_routing" in sql, (
        "migration must CREATE OR REPLACE the model-routing rollup view"
    )


@pytest.mark.unit
def test_migration_maps_empty_tier_to_not_tier_routed() -> None:
    """Empty cost_tier_name must be classified as the explicit not_tier_routed
    value -- never 'unknown', 'local', or silently dropped."""
    sql = _read_migration_sql()
    # The classification CASE must map empty tier to the explicit string.
    assert re.search(
        r"WHEN\s+cost_tier_name\s*=\s*''\s+THEN\s+'not_tier_routed'", sql
    ), "view must map empty cost_tier_name -> 'not_tier_routed'"
    # Guard against the rejected mis-classifications.
    assert "'unknown'" not in sql, "empty tier must not be bucketed as 'unknown'"


@pytest.mark.unit
def test_migration_excludes_not_tier_routed_from_pct_denominator() -> None:
    """The tier percentage denominator must count only LLM-tier-routed rows."""
    sql = _read_migration_sql()
    normalized = re.sub(r"\s+", " ", sql)
    # tier_routed_total = count of rows whose tier is non-empty (the denominator).
    assert "SUM(CASE WHEN cost_tier_name <> ''" in normalized, (
        "tier_routed_total denominator must filter on cost_tier_name <> ''"
    )
    assert "AS tier_routed_total" in normalized
    # not_tier_routed rows are counted separately, never folded into the denominator.
    assert "SUM(CASE WHEN cost_tier_name = ''" in normalized, (
        "not_tier_routed_count must count empty-tier rows separately"
    )
    assert "AS not_tier_routed_count" in normalized
    # pct is gated on the tier_routed flag so excluded rows get pct 0.
    assert "pct_of_tier_routed" in normalized
    assert re.search(
        r"WHEN tr\.tier_routed AND tt\.tier_routed_total > 0", normalized
    ), "pct_of_tier_routed must be computed only for tier-routed rows"


@pytest.mark.unit
def test_migration_surfaces_inclusive_total() -> None:
    normalized = re.sub(r"\s+", " ", _read_migration_sql())
    assert "COUNT(*)::int AS total_tasks" in normalized, (
        "total_tasks must count all rows (inclusive of not_tier_routed)"
    )
    assert "by_tier.summary AS by_tier" in normalized, (
        "the rollup must expose the tier distribution as a by_tier column"
    )


@pytest.mark.unit
def test_contract_model_routing_exposes_by_tier() -> None:
    exposure = _model_routing_exposure()
    columns = [str(c) for c in exposure["columns"]]
    json_columns = [str(c) for c in exposure.get("json_columns", [])]
    assert "by_tier" in columns, (
        "model-routing exposure must declare by_tier so the projection-API "
        f"returns the tier distribution; columns={columns}"
    )
    assert "by_tier" in json_columns, (
        "by_tier is a jsonb column and must be declared in json_columns so the "
        f"projection-API decodes it; json_columns={json_columns}"
    )


# ---------------------------------------------------------------------------
# 2. Reference math: the denominator semantics the SQL encodes.
# ---------------------------------------------------------------------------


def _reference_by_tier(tier_names: list[str]) -> dict[str, Any]:
    """Pure-Python mirror of the view's by_tier rollup semantics.

    Empty tier -> ``not_tier_routed`` (excluded from the percentage denominator,
    included in totals). This locks the intended behavior independently of the
    SQL engine.
    """
    total_tasks = len(tier_names)
    tier_routed_total = sum(1 for t in tier_names if t != "")
    not_tier_routed_count = sum(1 for t in tier_names if t == "")

    counts: dict[str, int] = {}
    for raw in tier_names:
        name = _NOT_TIER_ROUTED if raw == "" else raw
        counts[name] = counts.get(name, 0) + 1

    tiers = []
    for name, count in counts.items():
        tier_routed = name != _NOT_TIER_ROUTED
        pct = (
            (count / tier_routed_total) if (tier_routed and tier_routed_total) else 0.0
        )
        tiers.append(
            {
                "cost_tier_name": name,
                "count": count,
                "tier_routed": tier_routed,
                "pct_of_tier_routed": pct,
            }
        )
    return {
        "total_tasks": total_tasks,
        "tier_routed_total": tier_routed_total,
        "not_tier_routed_count": not_tier_routed_count,
        "tiers": tiers,
    }


# Mixed fixture: 2x local + 1x claude (LLM-tier-routed) + 2x delegate-skill
# (empty tier). This is the canonical "mixing LLM-tiered + delegate-skill rows"
# scenario the ticket requires proving.
_MIXED_FIXTURE = ["local", "local", "claude", "", ""]


@pytest.mark.unit
def test_reference_totals_are_inclusive() -> None:
    summary = _reference_by_tier(_MIXED_FIXTURE)
    assert summary["total_tasks"] == 5, "totals must include delegate-skill rows"


@pytest.mark.unit
def test_reference_not_tier_routed_counted_and_excluded() -> None:
    summary = _reference_by_tier(_MIXED_FIXTURE)
    assert summary["tier_routed_total"] == 3, "denominator excludes empty-tier rows"
    assert summary["not_tier_routed_count"] == 2
    not_routed = [
        t for t in summary["tiers"] if t["cost_tier_name"] == _NOT_TIER_ROUTED
    ]
    assert len(not_routed) == 1, "not_tier_routed must surface as a distinct bucket"
    assert not_routed[0]["tier_routed"] is False
    assert not_routed[0]["count"] == 2
    assert not_routed[0]["pct_of_tier_routed"] == 0.0


@pytest.mark.unit
def test_reference_tier_pct_sums_over_llm_rows_only() -> None:
    summary = _reference_by_tier(_MIXED_FIXTURE)
    routed = [t for t in summary["tiers"] if t["tier_routed"]]
    pct_sum = sum(t["pct_of_tier_routed"] for t in routed)
    assert pct_sum == pytest.approx(1.0), (
        "tier-routed percentages must sum to 1.0 (denominator = LLM-tier rows)"
    )
    by_name = {t["cost_tier_name"]: t for t in routed}
    assert by_name["local"]["pct_of_tier_routed"] == pytest.approx(2 / 3)
    assert by_name["claude"]["pct_of_tier_routed"] == pytest.approx(1 / 3)


@pytest.mark.unit
def test_reference_all_skill_rows_yields_empty_denominator() -> None:
    """Degenerate guard: a window with only delegate-skill rows must not divide
    by zero and must surface every row as not_tier_routed."""
    summary = _reference_by_tier(["", "", ""])
    assert summary["total_tasks"] == 3
    assert summary["tier_routed_total"] == 0
    assert summary["not_tier_routed_count"] == 3
    not_routed = [
        t for t in summary["tiers"] if t["cost_tier_name"] == _NOT_TIER_ROUTED
    ]
    assert not_routed[0]["pct_of_tier_routed"] == 0.0


# ---------------------------------------------------------------------------
# 3. Real-Postgres execution of the actual view DDL (skips without a DB).
# ---------------------------------------------------------------------------

_FIXTURE_ROWS = [
    # (correlation_id, delegated_to, model_name, cost_tier_name)
    ("corr-local-1", "qwen3-coder", "qwen3-coder-30b", "local"),
    ("corr-local-2", "qwen3-coder", "qwen3-coder-30b", "local"),
    ("corr-claude-1", "claude", "claude-opus-4", "claude"),
    # delegate-skill terminals: empty cost_tier_name (skill->node, not LLM tier)
    ("corr-skill-1", "delegate-skill", "", ""),
    ("corr-skill-2", "delegate-skill", "", ""),
]


async def _connect_or_skip() -> Any:
    """Connect to a reachable Postgres or skip.

    Self-contained (does not use the shared ``postgres_fixture``) so the test
    SKIPS — never ERRORS — when ``POSTGRES_PASSWORD`` is set in the environment
    but no broker/DB is reachable (the common CI-without-DB shape).
    """
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("POSTGRES_PASSWORD not set — skipping tier-distribution DB proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (
        OSError,
        asyncpg.PostgresError,
    ) as exc:  # pragma: no cover - infra-dependent
        pytest.skip(f"no reachable Postgres for tier-distribution DB proof: {exc}")


@pytest.mark.integration
async def test_view_classifies_not_tier_routed_against_real_postgres() -> None:
    """Execute the committed view DDL against a real Postgres fixture table and
    prove the not_tier_routed classification + denominator exclusion end to end.

    Runs only under ``-m integration`` with a reachable Postgres (self-skips
    otherwise). This is the deterministic, fixture-driven analogue of the dev
    psql DoD evidence.
    """
    conn = await _connect_or_skip()
    schema = "omn13662_tier_dist_test"

    await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    await conn.execute(f"CREATE SCHEMA {schema}")
    try:
        await conn.execute(f"SET search_path TO {schema}, public")
        # Minimal fixture table carrying every column the model-routing view
        # references. cost_tier_name mirrors migration 0018 (NOT NULL DEFAULT '').
        await conn.execute(
            """
            CREATE TABLE delegation_events (
                id BIGSERIAL PRIMARY KEY,
                correlation_id TEXT,
                task_type TEXT,
                delegated_to TEXT,
                model_name TEXT,
                quality_gate_passed BOOLEAN DEFAULT TRUE,
                latency_ms NUMERIC,
                delegation_latency_ms NUMERIC,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                cost_tier_name TEXT NOT NULL DEFAULT ''
            )
            """
        )
        for correlation_id, delegated_to, model_name, cost_tier_name in _FIXTURE_ROWS:
            await conn.execute(
                """
                INSERT INTO delegation_events
                    (correlation_id, task_type, delegated_to, model_name,
                     quality_gate_passed, latency_ms, cost_tier_name)
                VALUES ($1, 'code-review', $2, $3, TRUE, 100, $4)
                """,
                correlation_id,
                delegated_to,
                model_name,
                cost_tier_name,
            )

        # Apply the actual committed view DDL (resolves against the test schema
        # because of search_path) and read it back.
        await conn.execute(_read_migration_sql())
        raw = await conn.fetchval(
            "SELECT by_tier FROM projection_delegation_model_routing"
        )
        summary = json.loads(raw) if isinstance(raw, str) else raw

        assert summary["total_tasks"] == 5, "totals must include delegate-skill rows"
        assert summary["tier_routed_total"] == 3, "denominator excludes empty-tier rows"
        assert summary["not_tier_routed_count"] == 2

        tiers = {t["cost_tier_name"]: t for t in summary["tiers"]}
        assert tiers["not_tier_routed"]["tier_routed"] is False
        assert tiers["not_tier_routed"]["count"] == 2
        assert tiers["not_tier_routed"]["pct_of_tier_routed"] == 0.0

        routed_pct_sum = sum(
            t["pct_of_tier_routed"] for t in summary["tiers"] if t["tier_routed"]
        )
        assert routed_pct_sum == pytest.approx(1.0)
        assert tiers["local"]["pct_of_tier_routed"] == pytest.approx(2 / 3)
        assert tiers["claude"]["pct_of_tier_routed"] == pytest.approx(1 / 3)
    finally:
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await conn.close()
