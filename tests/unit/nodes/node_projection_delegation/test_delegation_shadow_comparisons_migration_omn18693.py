# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Source contract for restoring delegation shadow-comparison storage.

WHY THE 0044 RLS POLICY IS FAIL-CLOSED, NOT A BYPASS.

This note lives here rather than inside
``0044_restore_delegation_shadow_comparisons.sql`` on purpose. Those bytes are
already applied and are recorded in the live catalog under checksum
``3a1089294056fafeebbe5fdbe1c0910d3dc178b37d9402e2f37c19a32161c298``; the
node-migration vendor-parity gate byte-compares them against the vendored copy
in ``omnibase_infra`` with ``cmp``, so explanatory material cannot be carried in
the migration without breaking both. It is carried beside the test that proves
the behaviour instead.

``current_setting('app.tenant_id', true)`` returns NULL, never an empty string
and never a default, when the GUC is unset -- ``missing_ok=true`` only
suppresses the "unrecognized configuration parameter" error, it does not invent
a value. ``tenant_id = NULL`` therefore evaluates to NULL/unknown, and under
USING/WITH CHECK a non-true predicate excludes the row: an unset GUC denies
every read and every write, it does not admit them. A malformed, non-UUID-shaped
GUC value fails the ``::uuid`` cast and raises instead of silently coercing.
Both paths fail closed.

This is the same predicate shape, used and documented the same way, as
``node_canary_score_reducer/migrations/0003_capability_scores_tenant_id_to_uuid.sql``,
``node_hook_event_capture/migrations/0002_hook_events_tenant_rls.sql`` and
``node_projection_cost_summary/migrations/0002_llm_cost_aggregates_tenant_id_and_rls.sql``
-- it is not a one-off. ``test_omn18693_delegation_shadow_migration_real_postgres.py``
exercises it against a native PostgreSQL 16 instance with NOSUPERUSER
NOBYPASSRLS roles: the reader sees its own tenant's row and sees no row at all
for a cross-tenant or unset context.

Schema-level ``USAGE`` in the same migration is name resolution only. It grants
no table access and cannot bypass row-level security; the table privileges are
granted separately and the canonical tenant policy is both ENABLEd and FORCEd.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATION = (
    _REPO_ROOT
    / "src/omnimarket/nodes/node_projection_delegation/migrations"
    / "0044_restore_delegation_shadow_comparisons.sql"
)
_CONTRACT = _REPO_ROOT / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
_HANDLER = (
    _REPO_ROOT
    / "src/omnimarket/nodes/node_projection_delegation/handlers/handler_delegation.py"
)


def _migration() -> str:
    assert _MIGRATION.exists(), f"expected migration at {_MIGRATION}"
    return _MIGRATION.read_text(encoding="utf-8")


def test_restores_the_historical_payload_shape_with_tenant_identity_and_indexes() -> (
    None
):
    sql = _migration()
    for fragment in (
        "CREATE TABLE IF NOT EXISTS public.delegation_shadow_comparisons",
        "id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
        "correlation_id TEXT UNIQUE NOT NULL",
        "tenant_id UUID NOT NULL",
        "session_id TEXT",
        "timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "task_type TEXT NOT NULL",
        "primary_agent TEXT NOT NULL",
        "shadow_agent TEXT NOT NULL",
        "divergence_detected BOOLEAN DEFAULT false",
        "divergence_score NUMERIC(18, 9)",
        "primary_latency_ms INT",
        "shadow_latency_ms INT",
        "primary_cost_usd NUMERIC(18, 9)",
        "shadow_cost_usd NUMERIC(18, 9)",
        "divergence_reason TEXT",
        "created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()",
        "idx_delegation_shadow_comparisons_session_id",
        "idx_delegation_shadow_comparisons_timestamp",
    ):
        assert fragment in sql


def test_restoration_enforces_tenant_posture_and_reasserts_writer_grant() -> None:
    sql = _migration()
    executable = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "ENABLE ROW LEVEL SECURITY" in executable
    assert "FORCE ROW LEVEL SECURITY" in executable
    assert "CREATE POLICY tenant_isolation" in executable
    assert "current_setting('app.tenant_id', true)::uuid" in executable
    assert "ALTER COLUMN tenant_id DROP DEFAULT" in executable
    assert "tenant_id UUID NOT NULL DEFAULT" not in executable
    assert executable.lstrip().startswith("BEGIN;")
    assert executable.rstrip().endswith("COMMIT;")
    assert (
        "GRANT SELECT, INSERT, UPDATE ON public.delegation_shadow_comparisons "
        "TO tenant_projection_writer;" in executable
    )


def test_contract_points_at_restoration_and_preserves_tenant_domain() -> None:
    contract = _CONTRACT.read_text(encoding="utf-8")
    block = contract.split("- name: delegation_shadow_comparisons", 1)[1].split(
        "- name:", 1
    )[0]
    assert "schema: public" in block  # OMN-17887: the TENANT domain's schema
    assert 'migration: "0044_restore_delegation_shadow_comparisons.sql"' in block


def test_handler_insert_shape_matches_restored_columns_and_deduplication() -> None:
    handler = _HANDLER.read_text(encoding="utf-8")
    insert = handler.split("INSERT INTO {self._table_shadow}", 1)[1].split(
        "ON CONFLICT", 1
    )[0]
    for column in (
        "correlation_id",
        "session_id",
        "timestamp",
        "task_type",
        "primary_agent",
        "shadow_agent",
        "divergence_detected",
        "divergence_score",
        "primary_latency_ms",
        "shadow_latency_ms",
        "primary_cost_usd",
        "shadow_cost_usd",
        "divergence_reason",
        "tenant_id",
    ):
        assert column in insert
    assert "ON CONFLICT (correlation_id) DO NOTHING" in handler
    assert "$14" in insert
