# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14974: what migration 0027 expressed, and why it no longer holds.

SUPERSEDED IN PART BY OMN-18774. Every assertion below is about 0027's own
BYTES, which are frozen by the append-only migration ratchet and are therefore
still exactly what this file says they are. What is no longer true is the
SYSTEM claim the file's title used to make: generation projection access is
not tenant-scoped, because ``generation_events`` is declared
``schema: omninode_internal`` and the operator ruled on 2026-09-14
(``docs/tracking/ROLLING_WORK_LEDGER.md:654``) that an internal-classified
relation receives no tenant stamping and no row-level security.

0027 predates the classification manifest. Migration 0043 reverses its tenant
posture -- policy, RLS and column -- and the last test in this file asserts
that reversal so the two files cannot be read in isolation and disagree.
"""

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_NODE_DIR = _REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_projection_delegation"
_MIGRATION_NAME = "0027_generation_events_tenant_rls.sql"
_MIGRATION_PATH = _NODE_DIR / "migrations" / _MIGRATION_NAME
_EXPOSURE_ALLOWLIST = _REPO_ROOT / ".projection-exposure-allowlist.yaml"


def _normalized_sql() -> str:
    return " ".join(_MIGRATION_PATH.read_text().split())


def test_generation_events_contract_points_at_tenant_access_migration() -> None:
    contract = yaml.safe_load((_NODE_DIR / "contract.yaml").read_text())
    generation = next(
        table
        for table in contract["db_io"]["db_tables"]
        if table["name"] == "generation_events"
    )

    assert generation["migration"] == _MIGRATION_NAME


def test_generation_events_migration_adds_fail_closed_tenant_policy() -> None:
    sql = _normalized_sql()

    assert (
        "ALTER TABLE generation_events ADD COLUMN IF NOT EXISTS tenant_id text "
        "NOT NULL DEFAULT 'omninode'" in sql
    )
    assert "ALTER TABLE generation_events ENABLE ROW LEVEL SECURITY" in sql
    assert "ALTER TABLE generation_events FORCE ROW LEVEL SECURITY" not in sql
    assert "CREATE POLICY tenant_isolation ON generation_events" in sql
    assert "USING (tenant_id = current_setting('app.tenant_id', true))" in sql
    assert "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))" in sql


def test_generation_events_migration_grants_only_required_access() -> None:
    sql = _normalized_sql()

    assert "role_omnidash role missing" in sql
    assert "app_dashboard role missing" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON generation_events TO role_omnidash" in sql
    assert "GRANT SELECT ON generation_events TO app_dashboard" in sql
    assert "GRANT DELETE ON generation_events" not in sql


def test_absent_role_omnidash_warns_instead_of_aborting_the_migration() -> None:
    """OMN-15351: role_omnidash is environment-provisioned, so its absence WARNs.

    role_omnidash exists on cloud RDS (out-of-band) and on a compose cluster only
    when ROLE_OMNIDASH_PASSWORD was set at first-startup init; no migration
    creates it. A RAISE EXCEPTION on its absence made every local-lane deploy
    fatal at this file. Where the role exists the grants are unchanged.

    Execution proof in both role states (real psql against an ephemeral cluster)
    lives with the vendored copy that the deploy actually applies:
    omnibase_infra tests/integration/db/test_generation_events_role_tolerance_omn15351.py.
    """
    sql = _normalized_sql()

    assert "RAISE WARNING 'role_omnidash role missing" in sql
    assert "RAISE EXCEPTION 'role_omnidash role missing" not in sql
    # Not a silent skip: the warning names both grants it skips.
    assert "SKIPPING 2 grants" in sql
    assert "(1) GRANT USAGE ON SCHEMA public TO role_omnidash" in sql
    assert (
        "(2) GRANT SELECT, INSERT, UPDATE ON generation_events TO role_omnidash" in sql
    )
    # The grants themselves are guarded by the same existence check.
    assert (
        "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'role_omnidash') THEN "
        "EXECUTE 'GRANT USAGE ON SCHEMA public TO role_omnidash'; "
        "EXECUTE 'GRANT SELECT, INSERT, UPDATE ON generation_events TO role_omnidash';"
        in sql
    )


def test_app_dashboard_guard_stays_fail_closed() -> None:
    """OMN-15351 relaxed the role_omnidash guard ONLY.

    omnibase_infra forward migration 094 (OMN-14899) creates app_dashboard
    in-repo, so its absence is a migration-ordering bug, not an environment
    difference — the posture every sibling RLS migration takes.
    """
    sql = _normalized_sql()

    assert "RAISE EXCEPTION 'app_dashboard role missing" in sql
    assert "RAISE WARNING 'app_dashboard role missing" not in sql


def test_generation_events_tenant_key_is_an_explicit_exposure_omission() -> None:
    allowlist = yaml.safe_load(_EXPOSURE_ALLOWLIST.read_text())

    assert any(
        omission["node"] == "node_projection_delegation"
        and omission["table"] == "generation_events"
        and omission["column"] == "tenant_id"
        and "RLS" in omission["reason"]
        for omission in allowlist["omissions"]
    )


def test_the_tenant_posture_0027_added_is_reversed_by_0043_omn18774() -> None:
    """The successor exists, names this relation, and removes all three halves.

    Read as bytes rather than described: a reader who lands on 0027 through
    this file must be able to see, here, that its posture is not the live one.
    """
    successor = (
        _NODE_DIR / "migrations" / "0043_generation_events_drop_tenant_posture.sql"
    )
    assert successor.exists(), (
        "0027's tenant posture is reversed by 0043 (OMN-18774); if that file "
        "is gone, either this test or the reversal is wrong"
    )
    sql = " ".join(successor.read_text(encoding="utf-8").split())
    body = sql.split("$drop_generation_tenant_posture$", 1)[1]
    assert "DROP POLICY IF EXISTS tenant_isolation ON public.generation_events" in body
    assert "ALTER TABLE public.generation_events DISABLE ROW LEVEL SECURITY" in body
    assert (
        "ALTER TABLE public.generation_events DROP COLUMN IF EXISTS tenant_id" in body
    )
