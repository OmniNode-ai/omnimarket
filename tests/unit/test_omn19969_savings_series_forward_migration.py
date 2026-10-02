from __future__ import annotations

from pathlib import Path

MIGRATIONS = (
    Path(__file__).parents[2]
    / "src/omnimarket/nodes/node_projection_savings/migrations"
)
# 093, not 092: omnibase_infra already vendors an infra-side
# 092_restore_savings_views_security_invoker.sql into the same node folder, and the
# vendored copy of this migration must apply after it.
SERIES_MIGRATION = MIGRATIONS / "093_savings_series_persisted_baseline.sql"
SERIES_VIEW = "public.projection_delegation_savings_series"


def test_latest_savings_series_migration_uses_only_persisted_baseline_values() -> None:
    migration = SERIES_MIGRATION

    assert migration.is_file(), "a forward migration must replace the active view"
    sql = migration.read_text()

    assert sql.count(f"CREATE OR REPLACE VIEW {SERIES_VIEW} AS") == 1
    assert "model_cloud_baseline AS baseline_model" in sql
    assert "NULL::text AS baseline_model" in sql
    assert "claude-opus-4.1" not in sql


def test_event_only_rows_without_a_persisted_baseline_are_excluded() -> None:
    sql = SERIES_MIGRATION.read_text()
    event_branch = sql.split("SELECT event_sessions.*", maxsplit=1)[1].split(
        "classified_sessions AS", maxsplit=1
    )[0]

    assert "WHERE NOT EXISTS" in event_branch
    assert "AND event_sessions.baseline_model IS NOT NULL" in event_branch


def test_replacing_the_series_view_keeps_it_invoker_scoped() -> None:
    """CREATE OR REPLACE VIEW resets omitted view options, so a replacement that
    does not restate security_invoker makes the view read with its owner's rights
    and bypass the tenant row-level security on its base tables (OMN-19808)."""
    sql = SERIES_MIGRATION.read_text()
    replaced_at = sql.index(f"CREATE OR REPLACE VIEW {SERIES_VIEW} AS")
    reassert = f"ALTER VIEW {SERIES_VIEW} SET (security_invoker = true);"

    assert reassert in sql, "the replaced view must be set back to security_invoker"
    assert sql.index(reassert) > replaced_at, "the option must be set after the replace"


def test_no_other_migration_in_the_node_reuses_the_series_number() -> None:
    numbers = [p.name.split("_", 1)[0] for p in MIGRATIONS.glob("*.sql")]

    assert numbers.count("093") == 1
