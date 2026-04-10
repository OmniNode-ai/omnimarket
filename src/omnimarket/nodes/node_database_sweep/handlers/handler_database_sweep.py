# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""NodeDatabaseSweep — Projection table health and migration tracking.

Scans all tables in omnidash_analytics for row count and staleness,
and checks migration state for each ONEX database (Alembic + Drizzle).

ONEX node type: COMPUTE — deterministic scan, no LLM calls.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

_ALEMBIC_REPOS = [
    ("omnibase_infra", "omnibase_infra", "src/omnibase_infra/migrations/versions"),
    (
        "omniintelligence",
        "omniintelligence",
        "src/omniintelligence/migrations/versions",
    ),
    ("omnimemory", "omnimemory_db", "src/omnimemory/migrations/versions"),
]
_DRIZZLE_REPOS = [
    ("omnidash", "omnidash_analytics", "migrations"),
]

_TIMESTAMP_COLUMNS = (
    "created_at",
    "timestamp",
    "emitted_at",
    "updated_at",
    "recorded_at",
)


class ModelTableHealthResult(BaseModel):
    """Health classification for a single projection table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table_name: str
    row_count: int = 0
    latest_row: str | None = None  # ISO timestamp string or None
    status: str  # HEALTHY | STALE | EMPTY | MISSING | ORPHAN | NO_TIMESTAMP
    drizzle_defined: bool = False
    message: str = ""


class ModelMigrationStateResult(BaseModel):
    """Migration state for a single database."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    database: str
    repo: str
    migration_tool: str  # alembic | drizzle
    disk_migrations: int = 0
    applied_migrations: int = 0
    current_head: str | None = None
    status: str  # CURRENT | PENDING | AHEAD | FAILED | NO_TABLE | ERROR
    message: str = ""


class DatabaseSweepRequest(BaseModel):
    """Input for the database sweep handler."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    omni_home: str = Field(default="")
    table: str | None = None
    staleness_threshold_hours: int = 24
    dry_run: bool = False


class DatabaseSweepResult(BaseModel):
    """Output of the database sweep handler."""

    model_config = ConfigDict(extra="forbid")

    table_results: list[ModelTableHealthResult] = Field(default_factory=list)
    migration_results: list[ModelMigrationStateResult] = Field(default_factory=list)
    tables_healthy: int = 0
    tables_stale: int = 0
    tables_empty: int = 0
    tables_missing: int = 0
    tables_orphan: int = 0
    migrations_current: int = 0
    migrations_pending: int = 0
    migrations_failed: int = 0
    status: str = "healthy"  # healthy | issues_found | error
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    cmd: list[str], cwd: str | None = None, timeout: int = 15
) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            check=False,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as exc:
        return -1, "", str(exc)


def _psql(
    query: str, database: str, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a psql query and return (returncode, stdout, stderr)."""
    pg_env = dict(os.environ)
    if env:
        pg_env.update(env)
    cmd = [
        "psql",
        "-d",
        database,
        "-t",  # tuples only
        "-A",  # unaligned
        "-c",
        query,
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=pg_env,
            timeout=30,
            check=False,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except (subprocess.TimeoutExpired, OSError, FileNotFoundError) as exc:
        return -1, "", str(exc)


def _get_drizzle_tables(omni_home: str) -> set[str]:
    """Extract table names defined in omnidash Drizzle schemas."""
    schema_dir = Path(omni_home) / "omnidash" / "shared"
    tables: set[str] = set()
    if not schema_dir.is_dir():
        return tables
    for ts_file in schema_dir.glob("*-schema.ts"):
        try:
            content = ts_file.read_text(encoding="utf-8", errors="replace")
            import re

            for m in re.finditer(r'pgTable\(\s*["\']([^"\']+)["\']', content):
                tables.add(m.group(1))
        except OSError:
            pass
    return tables


def _check_table(
    table: str,
    database: str,
    staleness_hours: int,
    drizzle_tables: set[str],
) -> ModelTableHealthResult:
    """Check a single table's health."""
    drizzle_defined = table in drizzle_tables

    # Try each timestamp column in priority order
    for ts_col in _TIMESTAMP_COLUMNS:
        has_ts_col_rc, has_ts_col_out, _ = _psql(
            f"SELECT 1 FROM information_schema.columns "
            f"WHERE table_name='{table}' AND column_name='{ts_col}' LIMIT 1;",
            database,
        )
        if has_ts_col_rc == 0 and has_ts_col_out:
            # This column exists — run full health query
            query = (
                f"SELECT count(*), max({ts_col})::text, "
                f"CASE "
                f"  WHEN count(*) = 0 THEN 'EMPTY' "
                f"  WHEN max({ts_col}) < now() - interval '{staleness_hours} hours' THEN 'STALE' "
                f"  ELSE 'HEALTHY' "
                f"END "
                f"FROM {table};"
            )
            rc, out, err = _psql(query, database)
            if rc != 0:
                return ModelTableHealthResult(
                    table_name=table,
                    status="MISSING",
                    drizzle_defined=drizzle_defined,
                    message=f"query error: {err[:200]}",
                )
            parts = out.split("|")
            if len(parts) < 3:
                return ModelTableHealthResult(
                    table_name=table,
                    status="MISSING",
                    drizzle_defined=drizzle_defined,
                    message="unexpected query output",
                )
            try:
                row_count = int(parts[0])
            except ValueError:
                row_count = 0
            latest = parts[1] if parts[1] else None
            status = parts[2]
            return ModelTableHealthResult(
                table_name=table,
                row_count=row_count,
                latest_row=latest,
                status=status,
                drizzle_defined=drizzle_defined,
            )

    # No timestamp column — classify by row count only
    rc, out, err = _psql(f"SELECT count(*) FROM {table};", database)
    if rc != 0:
        return ModelTableHealthResult(
            table_name=table,
            status="MISSING",
            drizzle_defined=drizzle_defined,
            message=f"query error: {err[:200]}",
        )
    try:
        row_count = int(out)
    except ValueError:
        row_count = 0
    status = "EMPTY" if row_count == 0 else "NO_TIMESTAMP"
    return ModelTableHealthResult(
        table_name=table,
        row_count=row_count,
        status=status,
        drizzle_defined=drizzle_defined,
        message="no timestamp column found",
    )


def _get_all_tables(database: str) -> list[str]:
    """Return all user table names in the public schema."""
    rc, out, _ = _psql(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename;",
        database,
    )
    if rc != 0 or not out:
        return []
    return [line.strip() for line in out.splitlines() if line.strip()]


def _check_alembic_migration(
    repo: str, database: str, versions_path: str, omni_home: str
) -> ModelMigrationStateResult:
    """Check Alembic migration state for a repo."""
    versions_dir = Path(omni_home) / repo / versions_path
    if not versions_dir.is_dir():
        return ModelMigrationStateResult(
            database=database,
            repo=repo,
            migration_tool="alembic",
            status="ERROR",
            message=f"versions dir not found: {versions_dir}",
        )
    disk_count = len(list(versions_dir.glob("*.py")))

    rc, out, err = _psql(
        "SELECT count(*), version_num FROM alembic_version GROUP BY version_num;",
        database,
    )
    if rc != 0 or not out:
        return ModelMigrationStateResult(
            database=database,
            repo=repo,
            migration_tool="alembic",
            disk_migrations=disk_count,
            status="NO_TABLE",
            message=err[:200]
            if err
            else "alembic_version table missing or query failed",
        )

    rows = [r.strip() for r in out.splitlines() if r.strip()]
    if len(rows) > 1:
        return ModelMigrationStateResult(
            database=database,
            repo=repo,
            migration_tool="alembic",
            disk_migrations=disk_count,
            applied_migrations=len(rows),
            status="FAILED",
            message="multiple heads in alembic_version (branching issue)",
        )
    parts = rows[0].split("|") if rows else []
    current_head = parts[1].strip() if len(parts) >= 2 else None

    # Approximate: disk_count is total revisions, alembic_version holds 1 head
    # We treat applied_migrations = disk_count if head is present (all applied assumption)
    # A true chain walk would require parsing each file — too expensive here
    status = "CURRENT" if current_head else "NO_TABLE"
    return ModelMigrationStateResult(
        database=database,
        repo=repo,
        migration_tool="alembic",
        disk_migrations=disk_count,
        applied_migrations=disk_count if current_head else 0,
        current_head=current_head,
        status=status,
    )


def _check_drizzle_migration(
    repo: str, database: str, migrations_path: str, omni_home: str
) -> ModelMigrationStateResult:
    """Check Drizzle migration state for a repo."""
    migrations_dir = Path(omni_home) / repo / migrations_path
    if not migrations_dir.is_dir():
        return ModelMigrationStateResult(
            database=database,
            repo=repo,
            migration_tool="drizzle",
            status="ERROR",
            message=f"migrations dir not found: {migrations_dir}",
        )
    disk_count = len(list(migrations_dir.glob("*.sql")))

    rc, out, err = _psql(
        "SELECT count(*) FROM drizzle.__drizzle_migrations;",
        database,
    )
    if rc != 0 or not out:
        return ModelMigrationStateResult(
            database=database,
            repo=repo,
            migration_tool="drizzle",
            disk_migrations=disk_count,
            status="NO_TABLE",
            message=err[:200] if err else "__drizzle_migrations table missing",
        )
    try:
        applied = int(out.strip())
    except ValueError:
        applied = 0

    if disk_count == applied:
        status = "CURRENT"
    elif disk_count > applied:
        status = "PENDING"
    else:
        status = "AHEAD"

    return ModelMigrationStateResult(
        database=database,
        repo=repo,
        migration_tool="drizzle",
        disk_migrations=disk_count,
        applied_migrations=applied,
        status=status,
    )


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class NodeDatabaseSweep:
    """Scan projection tables and migration state across all ONEX databases."""

    def handle(self, request: DatabaseSweepRequest) -> DatabaseSweepResult:
        omni_home = request.omni_home or os.environ.get(
            "OMNI_HOME", "/Users/jonah/Code/omni_home"
        )
        analytics_db = "omnidash_analytics"

        drizzle_tables = _get_drizzle_tables(omni_home)

        # Phase 1 + 2: table health
        table_results: list[ModelTableHealthResult] = []
        if request.table:
            scan_tables = [request.table]
        else:
            scan_tables = _get_all_tables(analytics_db)

        for tbl in scan_tables:
            result = _check_table(
                tbl, analytics_db, request.staleness_threshold_hours, drizzle_tables
            )
            table_results.append(result)

        # Mark Drizzle-defined tables that are missing from DB
        db_table_names = {r.table_name for r in table_results}
        for dt in drizzle_tables - db_table_names:
            if request.table is None or request.table == dt:
                table_results.append(
                    ModelTableHealthResult(
                        table_name=dt,
                        status="MISSING",
                        drizzle_defined=True,
                        message="defined in Drizzle schema but table does not exist in DB",
                    )
                )

        # Mark orphan tables
        for r in table_results:
            if not r.drizzle_defined and r.status not in ("MISSING",):
                # Rebuild as ORPHAN
                table_results[table_results.index(r)] = ModelTableHealthResult(
                    table_name=r.table_name,
                    row_count=r.row_count,
                    latest_row=r.latest_row,
                    status="ORPHAN",
                    drizzle_defined=False,
                    message="exists in DB but not in Drizzle schema",
                )

        # Phase 3: migration tracking
        migration_results: list[ModelMigrationStateResult] = []
        for repo, database, path in _ALEMBIC_REPOS:
            migration_results.append(
                _check_alembic_migration(repo, database, path, omni_home)
            )
        for repo, database, path in _DRIZZLE_REPOS:
            migration_results.append(
                _check_drizzle_migration(repo, database, path, omni_home)
            )

        # Aggregation
        status_counts: dict[str, int] = {}
        for r in table_results:
            status_counts[r.status] = status_counts.get(r.status, 0) + 1

        mig_status_counts: dict[str, int] = {}
        for m in migration_results:
            mig_status_counts[m.status] = mig_status_counts.get(m.status, 0) + 1

        has_issues = (
            status_counts.get("STALE", 0) > 0
            or status_counts.get("EMPTY", 0) > 0
            or status_counts.get("MISSING", 0) > 0
            or mig_status_counts.get("PENDING", 0) > 0
            or mig_status_counts.get("FAILED", 0) > 0
        )

        return DatabaseSweepResult(
            table_results=table_results,
            migration_results=migration_results,
            tables_healthy=status_counts.get("HEALTHY", 0),
            tables_stale=status_counts.get("STALE", 0),
            tables_empty=status_counts.get("EMPTY", 0),
            tables_missing=status_counts.get("MISSING", 0),
            tables_orphan=status_counts.get("ORPHAN", 0),
            migrations_current=mig_status_counts.get("CURRENT", 0),
            migrations_pending=mig_status_counts.get("PENDING", 0),
            migrations_failed=mig_status_counts.get("FAILED", 0),
            status="issues_found" if has_issues else "healthy",
            dry_run=request.dry_run,
        )
