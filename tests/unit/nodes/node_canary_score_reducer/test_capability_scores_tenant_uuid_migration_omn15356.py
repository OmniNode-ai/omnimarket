# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Static shape ratchet for the capability_scores TEXT->UUID conversion (OMN-15356).

Mirrors the existing ``test_tenant_isolation_migrations_grant_app_dashboard_
select_omn14894.py`` static-scan style: cheap, no database required, and
regresses the moment the migration file's required shape changes without a
deliberate edit here. The live-database behavior (fail-closed on an unmapped
value, index/constraint continuity, GUC cast enforcement) is proven separately
by the Docker fixture harness in ``omnibase_infra``
(``docker/tenant-uuid-conversion-proof``) -- this file cannot and does not
claim to prove PostgreSQL execution semantics.

OMN-15732 AC2 split the original single 0003 file into two: 0003 defines the
new, schema-qualified, ownership-declared ``platform_catalog.
house_tenant_map_slug_to_uuid`` mapping function; 0004 does the
``ALTER COLUMN ... TYPE UUID`` conversion against the pre-existing
``public.capability_scores`` table using that function. The split exists so
the NEW function can be fully linted and ownership-checked by
omnibase_infra's application-database-domain-sql gate while the PRE-EXISTING
table's deferred-to-OMN-15359 schema exemption stays scoped to just the file
that still touches it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATIONS_DIR = (
    _REPO_ROOT / "src/omnimarket/nodes/node_canary_score_reducer/migrations"
)
_FUNCTION_MIGRATION = _MIGRATIONS_DIR / "0003_house_tenant_map_slug_to_uuid.sql"
_ALTER_MIGRATION = _MIGRATIONS_DIR / "0004_capability_scores_tenant_id_to_uuid.sql"


def _read_function_migration() -> str:
    assert _FUNCTION_MIGRATION.exists(), f"expected migration at {_FUNCTION_MIGRATION}"
    return _FUNCTION_MIGRATION.read_text(encoding="utf-8")


def _read_alter_migration() -> str:
    assert _ALTER_MIGRATION.exists(), f"expected migration at {_ALTER_MIGRATION}"
    return _ALTER_MIGRATION.read_text(encoding="utf-8")


def test_migration_files_exist_and_are_numbered_after_0002() -> None:
    numbers = sorted(
        int(path.name.split("_", 1)[0])
        for path in _MIGRATIONS_DIR.glob("*.sql")
        if path.name[:4].isdigit() or path.name[:3].isdigit()
    )
    assert 3 in numbers
    assert 4 in numbers
    assert max(numbers) >= 4


def test_function_migration_qualifies_the_function_into_platform_catalog() -> None:
    """OMN-15732 AC2: the new function must be schema-qualified, not public."""
    text = _read_function_migration()
    assert (
        "CREATE OR REPLACE FUNCTION platform_catalog.house_tenant_map_slug_to_uuid"
        in text
    )
    assert "CREATE SCHEMA IF NOT EXISTS platform_catalog" in text


def test_defines_the_fail_closed_mapping_function() -> None:
    text = _read_function_migration()
    assert "RAISE EXCEPTION" in text
    assert "'omninode'" in text
    assert "820272f9-4aaf-5add-a2df-0af942852ab2" in text


def test_converts_the_column_type_using_the_qualified_mapping_function() -> None:
    text = _read_alter_migration()
    assert "ALTER COLUMN tenant_id TYPE UUID" in text
    assert "USING platform_catalog.house_tenant_map_slug_to_uuid(tenant_id)" in text
    # the unqualified call must be gone entirely, not merely supplemented
    assert "USING house_tenant_map_slug_to_uuid(tenant_id)" not in text


def test_does_not_drop_or_recreate_the_tenant_id_index_or_unique_constraint() -> None:
    """The index/constraint-preservation claim in the migration header is
    only true if this migration never issues a DROP INDEX or DROP CONSTRAINT
    against them -- guard the claim against silent drift. Scans executable
    lines only (strips ``--`` comments first) so the header's own prose,
    which names the constraint to explain why it is untouched, cannot
    self-trigger this check."""
    executable = "\n".join(
        line
        for line in _read_alter_migration().splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "DROP INDEX" not in executable.upper()
    assert "DROP CONSTRAINT" not in executable.upper()
    assert "capability_scores_model_key_task_type_key" not in executable


def test_grants_app_dashboard_select_alongside_the_recreated_policy() -> None:
    """OMN-14894 ratchet: every file that (re)creates the ``tenant_isolation``
    policy must grant ``app_dashboard`` SELECT in the same file."""
    text = _read_alter_migration()
    assert "CREATE POLICY tenant_isolation ON" in text
    assert "GRANT SELECT ON public.capability_scores TO app_dashboard" in text


def test_rls_policy_casts_the_guc_to_uuid() -> None:
    text = _read_alter_migration()
    assert "current_setting('app.tenant_id', true)::uuid" in text
    # both the USING and WITH CHECK clauses must carry the cast, not just one
    assert text.count("current_setting('app.tenant_id', true)::uuid") >= 2


def test_conversion_guard_handles_uuid_text_and_unexpected_type() -> None:
    text = _read_alter_migration()
    assert "v_current_type = 'uuid'" in text
    assert "v_current_type = 'text'" in text
    assert "RAISE EXCEPTION" in text
