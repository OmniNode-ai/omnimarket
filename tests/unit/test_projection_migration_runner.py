# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Unit tests for the projection migration runner (OMN-11243).

Tests cover: discovery, ordering, checksum verification, dry-run, and
idempotency logic. DB-interaction tests use asyncpg mocks.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Load the dash-named script via importlib (Python cannot import dashes)
# ---------------------------------------------------------------------------
_SCRIPT_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "run-projection-migrations.py"
)
_spec = importlib.util.spec_from_file_location(
    "run_projection_migrations", _SCRIPT_PATH
)
assert _spec is not None
assert _spec.loader is not None
_mod = importlib.util.module_from_spec(_spec)
sys.modules["run_projection_migrations"] = _mod
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]

discover_migration_dirs = _mod.discover_migration_dirs
file_checksum = _mod.file_checksum
get_migration_files = _mod.get_migration_files
apply_migration = _mod.apply_migration

_MODULE_NAME = "run_projection_migrations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sql_file(tmp_path: Path, name: str, content: str = "SELECT 1;") -> Path:
    f = tmp_path / name
    f.write_text(content)
    return f


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


# ---------------------------------------------------------------------------
# file_checksum
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_file_checksum_matches_sha256(tmp_path: Path) -> None:
    content = "CREATE TABLE foo (id SERIAL PRIMARY KEY);"
    f = _make_sql_file(tmp_path, "0000_test.sql", content)
    assert file_checksum(f) == _sha256(content)


@pytest.mark.unit
def test_file_checksum_changes_on_content_change(tmp_path: Path) -> None:
    f = _make_sql_file(tmp_path, "0000_test.sql", "SELECT 1;")
    c1 = file_checksum(f)
    f.write_text("SELECT 2;")
    c2 = file_checksum(f)
    assert c1 != c2


# ---------------------------------------------------------------------------
# discover_migration_dirs
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_discover_finds_nodes_with_migrations_dir(tmp_path: Path) -> None:
    node_a = tmp_path / "node_alpha"
    node_b = tmp_path / "node_beta"
    (node_a / "migrations").mkdir(parents=True)
    (node_b / "migrations").mkdir(parents=True)
    (tmp_path / "node_gamma").mkdir()

    with patch.object(_mod, "NODES_ROOT", tmp_path):
        result = discover_migration_dirs()

    names = [name for name, _ in result]
    assert "node_alpha" in names
    assert "node_beta" in names
    assert "node_gamma" not in names


@pytest.mark.unit
def test_discover_sorted_by_node_name(tmp_path: Path) -> None:
    for name in ["node_zzz", "node_aaa", "node_mmm"]:
        (tmp_path / name / "migrations").mkdir(parents=True)

    with patch.object(_mod, "NODES_ROOT", tmp_path):
        result = discover_migration_dirs()

    names = [n for n, _ in result]
    assert names == sorted(names)


@pytest.mark.unit
def test_discover_node_filter(tmp_path: Path) -> None:
    for name in ["node_alpha", "node_beta"]:
        (tmp_path / name / "migrations").mkdir(parents=True)

    with patch.object(_mod, "NODES_ROOT", tmp_path):
        result = discover_migration_dirs(node_filter="node_alpha")

    assert len(result) == 1
    assert result[0][0] == "node_alpha"


@pytest.mark.unit
def test_discover_filter_no_match_returns_empty(tmp_path: Path) -> None:
    (tmp_path / "node_alpha" / "migrations").mkdir(parents=True)

    with patch.object(_mod, "NODES_ROOT", tmp_path):
        result = discover_migration_dirs(node_filter="node_nonexistent")

    assert result == []


@pytest.mark.unit
def test_discover_returns_empty_when_nodes_root_missing(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with patch.object(_mod, "NODES_ROOT", missing):
        result = discover_migration_dirs()
    assert result == []


# ---------------------------------------------------------------------------
# get_migration_files — ordering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_migration_files_sorted_lexicographically(tmp_path: Path) -> None:
    for name in ["0002_c.sql", "0000_a.sql", "0001_b.sql"]:
        (tmp_path / name).write_text("SELECT 1;")

    files = get_migration_files(tmp_path)
    names = [f.name for f in files]
    assert names == ["0000_a.sql", "0001_b.sql", "0002_c.sql"]


@pytest.mark.unit
def test_migration_files_only_sql(tmp_path: Path) -> None:
    (tmp_path / "0000_create.sql").write_text("SELECT 1;")
    (tmp_path / "README.md").write_text("docs")
    (tmp_path / "notes.txt").write_text("notes")

    files = get_migration_files(tmp_path)
    assert all(f.suffix == ".sql" for f in files)
    assert len(files) == 1


@pytest.mark.unit
def test_migration_files_empty_dir(tmp_path: Path) -> None:
    assert get_migration_files(tmp_path) == []


# ---------------------------------------------------------------------------
# apply_migration — checksum mismatch raises error
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_migration_checksum_mismatch_raises(tmp_path: Path) -> None:
    sql_file = _make_sql_file(tmp_path, "0000_create.sql", "SELECT 1;")
    stored_checksum = "aaaa" + "0" * 60  # different from actual file checksum

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"version": "0000_create.sql", "checksum": stored_checksum}]
    )

    with pytest.raises(RuntimeError, match="Checksum mismatch"):
        await apply_migration(conn, "node_test", sql_file, dry_run=False)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_migration_skips_when_checksum_matches(tmp_path: Path) -> None:
    content = "SELECT 1;"
    sql_file = _make_sql_file(tmp_path, "0000_create.sql", content)
    correct_checksum = _sha256(content)

    conn = AsyncMock()
    conn.fetch = AsyncMock(
        return_value=[{"version": "0000_create.sql", "checksum": correct_checksum}]
    )

    await apply_migration(conn, "node_test", sql_file, dry_run=False)
    conn.execute.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_migration_dry_run_does_not_execute(tmp_path: Path) -> None:
    sql_file = _make_sql_file(tmp_path, "0000_create.sql", "CREATE TABLE t(id INT);")
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])  # not yet applied

    await apply_migration(conn, "node_test", sql_file, dry_run=True)
    conn.execute.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_apply_migration_inserts_record_on_success(tmp_path: Path) -> None:
    content = "CREATE TABLE t(id INT);"
    sql_file = _make_sql_file(tmp_path, "0000_create.sql", content)
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])  # not yet applied

    await apply_migration(conn, "node_test", sql_file, dry_run=False)

    assert conn.execute.call_count == 2
    first_call_sql = conn.execute.call_args_list[0][0][0]
    assert "CREATE TABLE t" in first_call_sql
    second_call_sql = conn.execute.call_args_list[1][0][0]
    assert "omnimarket_schema_migrations" in second_call_sql


# ---------------------------------------------------------------------------
# contract_registry migration file exists and has correct DDL
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_contract_registry_migration_file_exists() -> None:
    migration_file = (
        Path(__file__).parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_contract_registry"
        / "migrations"
        / "0000_create_contract_registry.sql"
    )
    assert migration_file.exists(), f"Expected migration file at {migration_file}"


@pytest.mark.unit
def test_contract_registry_migration_creates_table() -> None:
    migration_file = (
        Path(__file__).parent.parent.parent
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_contract_registry"
        / "migrations"
        / "0000_create_contract_registry.sql"
    )
    sql = migration_file.read_text()
    assert "CREATE TABLE IF NOT EXISTS contract_registry" in sql
    assert "node_name" in sql
    assert "contract_hash" in sql
    assert "idx_contract_registry_node_name" in sql
    assert "idx_contract_registry_status" in sql
