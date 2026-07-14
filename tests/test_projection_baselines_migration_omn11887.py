"""OMN-11887: node-owned projection migration for node_projection_baselines.

node_projection_baselines declares four ``omnidash_analytics`` tables
(``baselines_snapshots``, ``baselines_comparisons``, ``baselines_trend``,
``baselines_breakdown``) under ``db_io.db_tables`` but shipped no migration:
every row pointed at ``0001_omnidash_analytics_read_model.sql``, a file that
exists in no repo, and the node had no ``migrations/`` directory. The
projection therefore could never materialize.

These tests pin the fix:

* a node-owned, idempotent migration that ``CREATE``s the four
  contract-declared tables (matching ``BaselinesProjectionRunner``'s writes),
* every ``db_io.db_tables[].migration`` reference resolves to a real file under
  the node's own ``migrations/`` directory, and
* the sibling ``node_projection_overnight`` contract no longer references the
  stale ``schema_overnight_sessions.sql`` filename.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
NODES = ROOT / "src" / "omnimarket" / "nodes"

BASELINES_DIR = NODES / "node_projection_baselines"
BASELINES_MIGRATIONS = BASELINES_DIR / "migrations"
BASELINES_MIGRATION = BASELINES_MIGRATIONS / "0001_create_baselines_tables.sql"
# OMN-14513: comparisons/trend/breakdown were recreated with the real
# producer row shapes (ModelBaselinesComparisonRow/TrendRow/BreakdownRow);
# 0002 is now the authoritative CREATE TABLE for those three tables.
BASELINES_MIGRATION_0002 = (
    BASELINES_MIGRATIONS / "0002_realign_child_tables_to_producer_schema.sql"
)

OVERNIGHT_DIR = NODES / "node_projection_overnight"

CONTRACT_DECLARED_TABLES = {
    "baselines_snapshots",
    "baselines_comparisons",
    "baselines_trend",
    "baselines_breakdown",
}

# Columns baselines_snapshots must have (0001 file, unchanged by OMN-14513 --
# HandlerProjectionBaselines/BaselinesProjectionRunner both write exactly the
# producer's top-level fields into this table).
REQUIRED_COLUMNS: dict[str, set[str]] = {
    "baselines_snapshots": {
        "snapshot_id",
        "contract_version",
        "computed_at_utc",
        "window_start_utc",
        "window_end_utc",
        "projected_at",
    },
}

# OMN-14513: columns comparisons/trend/breakdown must have (0002 file) --
# both HandlerProjectionBaselines and BaselinesProjectionRunner now write the
# producer's real ModelBaselinesComparisonRow/TrendRow/BreakdownRow field
# names into these tables.
REQUIRED_COLUMNS_0002: dict[str, set[str]] = {
    "baselines_comparisons": {
        "id",
        "snapshot_id",
        "comparison_date",
        "treatment_sessions",
        "treatment_success_rate",
        "control_sessions",
        "control_success_rate",
        "roi_pct",
        "sample_size",
        "computed_at",
        "created_at",
    },
    "baselines_trend": {
        "id",
        "snapshot_id",
        "trend_date",
        "cohort",
        "session_count",
        "success_rate",
        "roi_pct",
        "computed_at",
        "created_at",
    },
    "baselines_breakdown": {
        "id",
        "snapshot_id",
        "pattern_id",
        "pattern_label",
        "treatment_success_rate",
        "control_success_rate",
        "roi_pct",
        "sample_count",
        "computed_at",
        "created_at",
    },
}

_CREATE_TABLE_RE = re.compile(
    r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+(?P<name>[A-Za-z_][\w.]*)\s*\((?P<body>.*?)\n\)\s*;",
    re.IGNORECASE | re.DOTALL,
)


def _load_contract(node_dir: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.safe_load((node_dir / "contract.yaml").read_text())
    return loaded


def _db_tables(node_dir: Path) -> list[dict[str, Any]]:
    contract = _load_contract(node_dir)
    return list(contract.get("db_io", {}).get("db_tables", []))


def _strip_sql_comments(sql: str) -> str:
    """Drop ``--`` line comments so analysis sees only executable SQL."""
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


def _parse_create_tables(sql: str) -> dict[str, str]:
    """Return {table_name: column_body} for each CREATE TABLE IF NOT EXISTS."""
    tables: dict[str, str] = {}
    for match in _CREATE_TABLE_RE.finditer(sql):
        name = match.group("name").split(".")[-1].lower()
        tables[name] = match.group("body")
    return tables


def test_baselines_migration_file_exists() -> None:
    assert BASELINES_MIGRATIONS.is_dir(), (
        f"node_projection_baselines is missing a migrations/ directory: "
        f"{BASELINES_MIGRATIONS}"
    )
    assert BASELINES_MIGRATION.is_file(), (
        f"expected node-owned migration at {BASELINES_MIGRATION}"
    )


def test_baselines_migration_is_idempotent() -> None:
    sql = _strip_sql_comments(BASELINES_MIGRATION.read_text())
    # Every CREATE TABLE / CREATE INDEX must be guarded so the migration is
    # safe to re-apply against an already-seeded omnidash_analytics database.
    creates = re.findall(r"CREATE\s+TABLE", sql, re.IGNORECASE)
    guarded = re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", sql, re.IGNORECASE)
    assert creates, "migration declares no CREATE TABLE statements"
    assert len(creates) == len(guarded), (
        "every CREATE TABLE must use IF NOT EXISTS for re-runnable idempotency"
    )
    bare_index = re.findall(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)", sql, re.IGNORECASE
    )
    assert not bare_index, "every CREATE INDEX must use IF NOT EXISTS"


def test_baselines_migration_creates_contract_tables() -> None:
    sql = BASELINES_MIGRATION.read_text()
    tables = _parse_create_tables(sql)
    missing = CONTRACT_DECLARED_TABLES - set(tables)
    assert not missing, f"migration does not create contract tables: {sorted(missing)}"


def test_baselines_migration_columns_match_projection_writes() -> None:
    sql = BASELINES_MIGRATION.read_text()
    tables = _parse_create_tables(sql)
    for table, required in REQUIRED_COLUMNS.items():
        body = tables.get(table, "")
        assert body, f"migration missing CREATE TABLE for {table}"
        # Column tokens appear as the first identifier on a definition line.
        present = set(re.findall(r"(?m)^\s*([a-z_][a-z0-9_]*)\b", body))
        missing = required - present
        assert not missing, (
            f"{table} migration missing columns the projection writes: "
            f"{sorted(missing)}"
        )


def test_baselines_migration_0002_file_exists() -> None:
    assert BASELINES_MIGRATION_0002.is_file(), (
        f"expected the OMN-14513 realignment migration at {BASELINES_MIGRATION_0002}"
    )


def test_baselines_migration_0002_is_idempotent() -> None:
    sql = _strip_sql_comments(BASELINES_MIGRATION_0002.read_text())
    creates = re.findall(r"CREATE\s+TABLE", sql, re.IGNORECASE)
    guarded = re.findall(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS", sql, re.IGNORECASE)
    assert creates, "0002 migration declares no CREATE TABLE statements"
    assert len(creates) == len(guarded), (
        "every CREATE TABLE must use IF NOT EXISTS for re-runnable idempotency"
    )
    drops = re.findall(r"DROP\s+TABLE", sql, re.IGNORECASE)
    guarded_drops = re.findall(r"DROP\s+TABLE\s+IF\s+EXISTS", sql, re.IGNORECASE)
    assert len(drops) == len(guarded_drops), (
        "every DROP TABLE must use IF EXISTS for re-runnable idempotency"
    )
    bare_index = re.findall(
        r"CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?!IF\s+NOT\s+EXISTS)", sql, re.IGNORECASE
    )
    assert not bare_index, "every CREATE INDEX must use IF NOT EXISTS"


def test_baselines_migration_0002_columns_match_producer_row_shapes() -> None:
    """OMN-14513: 0002's columns must hold the real producer row fields.

    This is the regression pin for the seam-mismatch fix -- the previous
    0001-only schema (pattern_id/token_delta/date/avg_cost_savings/action/
    count) shared almost no field names with
    ModelBaselinesComparisonRow/ModelBaselinesTrendRow/
    ModelBaselinesBreakdownRow.
    """
    sql = BASELINES_MIGRATION_0002.read_text()
    tables = _parse_create_tables(sql)
    for table, required in REQUIRED_COLUMNS_0002.items():
        body = tables.get(table, "")
        assert body, f"0002 migration missing CREATE TABLE for {table}"
        present = set(re.findall(r"(?m)^\s*([a-z_][a-z0-9_]*)\b", body))
        missing = required - present
        assert not missing, (
            f"{table} (0002) migration missing producer-shaped columns: "
            f"{sorted(missing)}"
        )


def test_baselines_contract_migration_refs_resolve() -> None:
    rows = _db_tables(BASELINES_DIR)
    assert rows, "node_projection_baselines must declare db_tables"
    for row in rows:
        migration = row["migration"]
        assert migration != "0001_omnidash_analytics_read_model.sql", (
            f"db_tables row {row['name']!r} still references the nonexistent "
            "0001_omnidash_analytics_read_model.sql"
        )
        assert (BASELINES_MIGRATIONS / migration).is_file(), (
            f"db_tables row {row['name']!r} references a missing migration file: "
            f"{migration}"
        )


def test_overnight_contract_migration_refs_resolve() -> None:
    migrations_dir = OVERNIGHT_DIR / "migrations"
    rows = _db_tables(OVERNIGHT_DIR)
    assert rows, "node_projection_overnight must declare db_tables"
    for row in rows:
        migration = row["migration"]
        assert migration != "schema_overnight_sessions.sql", (
            f"overnight db_tables row {row['name']!r} still references the stale "
            "schema_overnight_sessions.sql filename"
        )
        assert (migrations_dir / migration).is_file(), (
            f"overnight db_tables row {row['name']!r} references a missing "
            f"migration file: {migration}"
        )
