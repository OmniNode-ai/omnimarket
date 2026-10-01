from __future__ import annotations

from pathlib import Path

MIGRATIONS = (
    Path(__file__).parents[2]
    / "src/omnimarket/nodes/node_projection_savings/migrations"
)


def test_latest_savings_series_migration_uses_only_persisted_baseline_values() -> None:
    migration = MIGRATIONS / "092_savings_series_persisted_baseline.sql"

    assert migration.is_file(), "a forward migration must replace the active view"
    sql = migration.read_text()

    assert (
        sql.count(
            "CREATE OR REPLACE VIEW public.projection_delegation_savings_series AS"
        )
        == 1
    )
    assert "model_cloud_baseline AS baseline_model" in sql
    assert "NULL::text AS baseline_model" in sql
    assert "claude-opus-4.1" not in sql


def test_event_only_rows_without_a_persisted_baseline_are_excluded() -> None:
    migration = MIGRATIONS / "092_savings_series_persisted_baseline.sql"
    sql = migration.read_text()
    event_branch = sql.split("SELECT event_sessions.*", maxsplit=1)[1].split(
        "classified_sessions AS", maxsplit=1
    )[0]

    assert "WHERE NOT EXISTS" in event_branch
    assert "AND event_sessions.baseline_model IS NOT NULL" in event_branch
