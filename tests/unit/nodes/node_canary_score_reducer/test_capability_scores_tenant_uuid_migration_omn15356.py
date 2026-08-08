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

OMN-15732 AC2 adjudication (2026-08-08): an earlier revision split the
mapping into its own schema-qualified `platform_catalog.
house_tenant_map_slug_to_uuid` catalog function so it could carry an
ownership declaration. That placement was rejected -- `platform_catalog` is
inadmissible for a `node:%` migration stream at both the static domain gate
and the runtime ledger CHECK constraint in omnibase_infra, and the function
had exactly one DDL-time consumer and no runtime caller. The mapping is now
inlined directly into this single migration with no CREATE FUNCTION and no
new schema-qualified object; there is no `0004` file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATIONS_DIR = (
    _REPO_ROOT / "src/omnimarket/nodes/node_canary_score_reducer/migrations"
)
_MIGRATION = _MIGRATIONS_DIR / "0003_capability_scores_tenant_id_to_uuid.sql"


def _read_migration() -> str:
    assert _MIGRATION.exists(), f"expected migration at {_MIGRATION}"
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_file_exists_and_is_numbered_after_0002() -> None:
    numbers = sorted(
        int(path.name.split("_", 1)[0])
        for path in _MIGRATIONS_DIR.glob("*.sql")
        if path.name[:4].isdigit() or path.name[:3].isdigit()
    )
    assert 3 in numbers
    # OMN-15732 AC2 adjudication: no 0004 -- the split was reverted.
    assert 4 not in numbers


def test_defines_no_persistent_mapping_function_or_new_schema() -> None:
    """OMN-15732 AC2 adjudication: no catalog object escapes ownership
    declaration because none is created -- the mapping is inlined. Scans
    executable lines only (strips ``--`` comments first) so the header's own
    prose, which explains why there is no CREATE FUNCTION, cannot
    self-trigger this check."""
    executable = "\n".join(
        line
        for line in _read_migration().splitlines()
        if not line.lstrip().startswith("--")
    ).upper()
    assert "CREATE FUNCTION" not in executable
    assert "CREATE OR REPLACE FUNCTION" not in executable
    assert "PLATFORM_CATALOG" not in executable
    assert "CREATE SCHEMA" not in executable


def test_has_a_preguard_raise_for_unmapped_tenant_values() -> None:
    text = _read_migration()
    assert "RAISE EXCEPTION" in text
    assert "'omninode'" in text
    assert "820272f9-4aaf-5add-a2df-0af942852ab2" in text
    assert "IS DISTINCT FROM 'omninode'" in text


def test_converts_the_column_type_with_a_case_expression_and_no_else() -> None:
    """The `USING` clause must be a bare CASE with no ELSE branch: an
    unmapped value evaluates to NULL and is rejected by the column's NOT
    NULL constraint -- the second, independent fail-closed guard."""
    text = _read_migration()
    assert "ALTER COLUMN tenant_id TYPE UUID" in text
    assert "CASE tenant_id" in text
    assert "WHEN 'omninode' THEN" in text
    # No ELSE branch anywhere in the executable CASE expression.
    executable = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    case_start = executable.index("CASE tenant_id")
    case_end = executable.index("END", case_start)
    assert "ELSE" not in executable[case_start:case_end]


def test_does_not_drop_or_recreate_the_tenant_id_index_or_unique_constraint() -> None:
    """The index/constraint-preservation claim in the migration header is
    only true if this migration never issues a DROP INDEX or DROP CONSTRAINT
    against them -- guard the claim against silent drift. Scans executable
    lines only (strips ``--`` comments first) so the header's own prose,
    which names the constraint to explain why it is untouched, cannot
    self-trigger this check."""
    executable = "\n".join(
        line
        for line in _read_migration().splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "DROP INDEX" not in executable.upper()
    assert "DROP CONSTRAINT" not in executable.upper()
    assert "capability_scores_model_key_task_type_key" not in executable


def test_grants_app_dashboard_select_alongside_the_recreated_policy() -> None:
    """OMN-14894 ratchet: every file that (re)creates the ``tenant_isolation``
    policy must grant ``app_dashboard`` SELECT in the same file."""
    text = _read_migration()
    assert "CREATE POLICY tenant_isolation ON" in text
    assert "GRANT SELECT ON public.capability_scores TO app_dashboard" in text


def test_rls_policy_casts_the_guc_to_uuid() -> None:
    text = _read_migration()
    assert "current_setting('app.tenant_id', true)::uuid" in text
    # both the USING and WITH CHECK clauses must carry the cast, not just one
    assert text.count("current_setting('app.tenant_id', true)::uuid") >= 2


def test_conversion_guard_handles_uuid_text_and_unexpected_type() -> None:
    text = _read_migration()
    assert "v_current_type = 'uuid'" in text
    assert "v_current_type = 'text'" in text
    assert "RAISE EXCEPTION" in text
