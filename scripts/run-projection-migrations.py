# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""
Migration runner for omnimarket projection nodes.

Discovers migrations/ directories under src/omnimarket/nodes/*/,
applies SQL files in filename order, and tracks state in the
omnimarket_schema_migrations table.

Usage:
    uv run python scripts/run-projection-migrations.py --dry-run
    uv run python scripts/run-projection-migrations.py
    uv run python scripts/run-projection-migrations.py --node node_projection_registration
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import sys
from pathlib import Path

import asyncpg

REPO_ROOT = Path(__file__).parent.parent
NODES_ROOT = REPO_ROOT / "src" / "omnimarket" / "nodes"

CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS omnimarket_schema_migrations (
    id SERIAL PRIMARY KEY,
    node_name TEXT NOT NULL,
    version TEXT NOT NULL,
    filename TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(node_name, version)
);
"""


def file_checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def discover_migration_dirs(node_filter: str | None = None) -> list[tuple[str, Path]]:
    """Return [(node_name, migrations_dir), ...] sorted by node_name."""
    results: list[tuple[str, Path]] = []
    if not NODES_ROOT.exists():
        return results
    for node_dir in sorted(NODES_ROOT.iterdir()):
        if not node_dir.is_dir():
            continue
        if node_filter and node_dir.name != node_filter:
            continue
        migrations_dir = node_dir / "migrations"
        if migrations_dir.is_dir():
            results.append((node_dir.name, migrations_dir))
    return results


def get_migration_files(migrations_dir: Path) -> list[Path]:
    """Return SQL files sorted by filename (lexicographic order)."""
    return sorted(migrations_dir.glob("*.sql"), key=lambda f: f.name)


async def ensure_migrations_table(conn: asyncpg.Connection) -> None:
    await conn.execute(CREATE_MIGRATIONS_TABLE)


async def get_applied_migrations(
    conn: asyncpg.Connection,
    node_name: str,
) -> dict[str, str]:
    """Return {version: checksum} for all applied migrations for this node."""
    rows = await conn.fetch(
        "SELECT version, checksum FROM omnimarket_schema_migrations WHERE node_name = $1",
        node_name,
    )
    return {r["version"]: r["checksum"] for r in rows}


async def apply_migration(
    conn: asyncpg.Connection,
    node_name: str,
    sql_file: Path,
    dry_run: bool,
) -> None:
    version = sql_file.name
    checksum = file_checksum(sql_file)
    applied = await get_applied_migrations(conn, node_name)

    if version in applied:
        if applied[version] != checksum:
            raise RuntimeError(
                f"Checksum mismatch for already-applied migration "
                f"{node_name}/{version}: "
                f"stored={applied[version]!r}, file={checksum!r}. "
                f"Schema drift detected — manual intervention required."
            )
        return

    if dry_run:
        print(f"  [dry-run] would apply: {node_name}/{version}")
        return

    sql = sql_file.read_text()
    await conn.execute(sql)
    await conn.execute(
        """
        INSERT INTO omnimarket_schema_migrations
            (node_name, version, filename, checksum)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (node_name, version) DO NOTHING
        """,
        node_name,
        version,
        sql_file.name,
        checksum,
    )
    print(f"  applied: {node_name}/{version}")


async def run(
    db_url: str,
    dry_run: bool,
    node_filter: str | None,
) -> int:
    """Apply all pending migrations. Returns count applied (or pending if dry_run)."""
    conn = await asyncpg.connect(db_url)
    try:
        await ensure_migrations_table(conn)

        migration_dirs = discover_migration_dirs(node_filter)
        if not migration_dirs:
            if node_filter:
                print(f"No migrations directory found for node: {node_filter}")
            else:
                print("No migration directories found under nodes/.")
            return 0

        total = 0
        for node_name, migrations_dir in migration_dirs:
            files = get_migration_files(migrations_dir)
            if not files:
                continue

            applied = await get_applied_migrations(conn, node_name)
            pending = [f for f in files if f.name not in applied]

            if not pending:
                continue

            print(f"\n{node_name}: {len(pending)} pending migration(s)")
            for sql_file in pending:
                await apply_migration(conn, node_name, sql_file, dry_run)
                total += 1

        if total == 0:
            print("No pending migrations.")
        elif dry_run:
            print(f"\n[dry-run] {total} migration(s) would be applied.")
        else:
            print(f"\nMigration complete: {total} applied.")

        return total
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply pending omnimarket projection node migrations"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print pending migrations without applying",
    )
    parser.add_argument(
        "--node",
        default=None,
        help="Run migrations for a single node only (e.g. node_projection_registration)",
    )
    parser.add_argument(
        "--db-url",
        default=os.environ.get("OMNIMARKET_DB_URL"),
        help="PostgreSQL connection URL (default: OMNIMARKET_DB_URL env var)",
    )
    args = parser.parse_args()

    if not args.db_url:
        print("ERROR: --db-url or OMNIMARKET_DB_URL required", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(args.db_url, args.dry_run, args.node))


if __name__ == "__main__":
    main()
