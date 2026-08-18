# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Static shape ratchet for the delegation_events TEXT->UUID conversion
(OMN-15683).

Mirrors ``test_capability_scores_tenant_uuid_migration_omn15356.py``: cheap,
no database required, and regresses the moment the migration file's required
shape changes without a deliberate edit here. The live-database execution
semantics (fail-closed on an unmapped value, RLS cast, both writer sites
resolving the same identity) are proven separately by
``tests/test_omn15683_tenant_uuid_migration_real_postgres.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATIONS_DIR = (
    _REPO_ROOT / "src/omnimarket/nodes/node_projection_delegation/migrations"
)
_MIGRATION = _MIGRATIONS_DIR / "0031_delegation_events_tenant_id_to_uuid.sql"


def _read_migration() -> str:
    assert _MIGRATION.exists(), f"expected migration at {_MIGRATION}"
    return _MIGRATION.read_text(encoding="utf-8")


def test_migration_file_exists_and_is_numbered_after_0030() -> None:
    # Some files in this directory carry a non-numeric suffix on the prefix
    # (e.g. "0009a_..."), so only a purely-digit prefix is a sortable
    # migration number.
    numbers = sorted(
        int(prefix)
        for path in _MIGRATIONS_DIR.glob("*.sql")
        for prefix in (path.name.split("_", 1)[0],)
        if prefix.isdigit()
    )
    assert 31 in numbers
    assert 30 in numbers


def test_has_a_preguard_raise_for_unmapped_tenant_values() -> None:
    text = _read_migration()
    assert "RAISE EXCEPTION" in text
    assert "'omninode'" in text
    assert "'beta-business-proof'" in text
    assert "'beta-gateway-canary-79afa7263852'" in text
    assert "NOT IN (" in text


def test_deletes_the_known_test_and_seed_rows_by_exact_correlation_id_before_the_preguard() -> (
    None
):
    """OMN-15683: the live onex-dev residual (8 rows under seed/e2e/spotcheck
    identifiers, live-enumerated 2026-08-18, none a real provisioned tenant)
    is dispositioned by exact correlation_id -- never a sweeping tenant_id
    predicate -- so the pre-guard has zero exceptions on apply. The DELETE
    must appear before the pre-guard's DO block in the file."""
    text = _read_migration()
    delete_idx = text.index("DELETE FROM delegation_events")
    preguard_idx = text.index("Pre-guard: refuse before any DDL runs")
    assert delete_idx < preguard_idx
    for correlation_id in (
        "SEED-A-1",
        "SEED-A-2",
        "SEED-A-3",
        "SEED-B-1",
        "SEED-B-2",
        "SEED-B-3",
        "4ad8a332-4e45-490e-99f4-53e61e8fa05c",
        "549c8c93-1e9a-40dd-917f-9b323fa94b1f",
    ):
        assert f"'{correlation_id}'" in text


def test_converts_the_column_type_with_a_case_expression_and_no_else() -> None:
    """The `USING` clause must be a bare CASE with no ELSE branch: an
    unmapped value evaluates to NULL and is rejected by the column's NOT
    NULL constraint -- the second, independent fail-closed guard."""
    text = _read_migration()
    assert "ALTER COLUMN tenant_id TYPE UUID" in text
    assert "CASE tenant_id" in text
    assert "WHEN 'omninode' THEN" in text
    assert "WHEN 'beta-business-proof' THEN" in text
    assert "WHEN 'beta-gateway-canary-79afa7263852' THEN" in text
    executable = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    case_start = executable.index("CASE tenant_id")
    case_end = executable.index("END", case_start)
    assert "ELSE" not in executable[case_start:case_end]


def test_maps_the_two_provisioned_beta_tenants_to_their_live_verified_uuids() -> None:
    """Pins the exact UUIDs this migration converts to against the
    OMN-15683 live probe (omninode_cloud.public.tenants, re-verified
    2026-08-18). A hand-edited literal on either side fails this test."""
    text = _read_migration()
    assert "91c74442-1233-4c97-b191-911a10346fdf" in text  # beta-business-proof
    assert "79afa726-3852-464f-b7a4-d4b8b9c75ee7" in text  # beta-gateway-canary
    assert "820272f9-4aaf-5add-a2df-0af942852ab2" in text  # house tenant


def test_does_not_drop_or_recreate_the_tenant_id_index() -> None:
    executable = "\n".join(
        line
        for line in _read_migration().splitlines()
        if not line.lstrip().startswith("--")
    )
    assert "DROP INDEX" not in executable.upper()
    assert "DROP CONSTRAINT" not in executable.upper()
    assert "idx_delegation_events_tenant_id" not in executable


def test_grants_app_dashboard_select_alongside_the_recreated_policy() -> None:
    text = _read_migration()
    assert "CREATE POLICY tenant_isolation ON" in text
    assert "GRANT SELECT ON delegation_events TO app_dashboard" in text


def test_rls_policy_casts_the_guc_to_uuid() -> None:
    text = _read_migration()
    assert "current_setting('app.tenant_id', true)::uuid" in text
    assert text.count("current_setting('app.tenant_id', true)::uuid") >= 2


def test_conversion_guard_handles_uuid_text_and_unexpected_type() -> None:
    text = _read_migration()
    assert "v_current_type = 'uuid'" in text
    assert "v_current_type = 'text'" in text
    assert "RAISE EXCEPTION" in text


def test_ownership_manifest_authoritative_source_points_at_this_migration() -> None:
    """OMN-15683: an unwired migration (touching a table's schema without
    updating scripts/application-relation-ownership.yaml's authoritative
    source) was flagged as a known foot-gun class in this same sweep --
    guard against reintroducing it for this specific migration."""
    manifest = (_REPO_ROOT / "scripts/application-relation-ownership.yaml").read_text(
        encoding="utf-8"
    )
    assert (
        "src/omnimarket/nodes/node_projection_delegation/migrations/"
        "0031_delegation_events_tenant_id_to_uuid.sql" in manifest
    )
    assert (
        "0029_delegation_terminal_failure_cause.sql" not in manifest
        or "0031_delegation_events_tenant_id_to_uuid.sql" in manifest
    )
