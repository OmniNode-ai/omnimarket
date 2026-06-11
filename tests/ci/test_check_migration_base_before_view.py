# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the migration base-table-before-view CI gate (OMN-12942)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.ci.check_migration_base_before_view import (
    _check_node,
    _referenced_relations,
    main,
)


def _write_node_migrations(root: Path, node: str, files: dict[str, str]) -> Path:
    migrations = root / "src" / "omnimarket" / "nodes" / node / "migrations"
    migrations.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (migrations / name).write_text(body)
    return migrations


# ---------------------------------------------------------------------------
# _referenced_relations
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_referenced_relations_extracts_from_and_join() -> None:
    body = "SELECT * FROM base_a JOIN base_b ON base_a.id = base_b.id"
    assert _referenced_relations(body) == {"base_a", "base_b"}


@pytest.mark.unit
def test_referenced_relations_ignores_ctes() -> None:
    body = (
        "WITH totals AS (SELECT 1 FROM base_a) SELECT * FROM totals JOIN base_a ON TRUE"
    )
    # `totals` is a CTE; only base_a is a real relation.
    assert _referenced_relations(body) == {"base_a"}


@pytest.mark.unit
def test_referenced_relations_ignores_extract_from() -> None:
    # EXTRACT(EPOCH FROM created_at) must not register `created_at` as a relation.
    body = "SELECT EXTRACT(EPOCH FROM created_at) FROM base_a"
    assert _referenced_relations(body) == {"base_a"}


@pytest.mark.unit
def test_referenced_relations_ignores_subquery_from() -> None:
    body = "SELECT * FROM (SELECT 1 FROM base_a) sub"
    assert _referenced_relations(body) == {"base_a"}


# ---------------------------------------------------------------------------
# _check_node — the ordering invariant
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_view_over_uncreated_table_is_flagged(tmp_path: Path) -> None:
    migrations = _write_node_migrations(
        tmp_path,
        "node_example",
        {
            "0001_create_view.sql": (
                "CREATE OR REPLACE VIEW v_example AS SELECT * FROM missing_base_table;"
            ),
        },
    )
    violations = _check_node(migrations, tmp_path)
    assert len(violations) == 1
    assert "missing_base_table" in violations[0]
    assert "v_example" in violations[0]


@pytest.mark.unit
def test_base_table_before_view_is_clean(tmp_path: Path) -> None:
    migrations = _write_node_migrations(
        tmp_path,
        "node_example",
        {
            "0000_create_base.sql": ("CREATE TABLE IF NOT EXISTS base_table (id INT);"),
            "0001_create_view.sql": (
                "CREATE OR REPLACE VIEW v_example AS SELECT * FROM base_table;"
            ),
        },
    )
    assert _check_node(migrations, tmp_path) == []


@pytest.mark.unit
def test_base_table_after_view_is_flagged(tmp_path: Path) -> None:
    # Lexical order means the view (0000) runs before the table (0001) -> FAIL.
    migrations = _write_node_migrations(
        tmp_path,
        "node_example",
        {
            "0000_create_view.sql": (
                "CREATE OR REPLACE VIEW v_example AS SELECT * FROM base_table;"
            ),
            "0001_create_base.sql": ("CREATE TABLE IF NOT EXISTS base_table (id INT);"),
        },
    )
    violations = _check_node(migrations, tmp_path)
    assert len(violations) == 1
    assert "base_table" in violations[0]


@pytest.mark.unit
def test_view_over_earlier_view_is_clean(tmp_path: Path) -> None:
    migrations = _write_node_migrations(
        tmp_path,
        "node_example",
        {
            "0000_create_base.sql": "CREATE TABLE base_table (id INT);",
            "0001_view_a.sql": (
                "CREATE OR REPLACE VIEW v_a AS SELECT * FROM base_table;"
            ),
            "0002_view_b.sql": "CREATE OR REPLACE VIEW v_b AS SELECT * FROM v_a;",
        },
    )
    assert _check_node(migrations, tmp_path) == []


# ---------------------------------------------------------------------------
# main — runs against the real repo and must be clean (regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_main_clean_on_real_repo() -> None:
    # The shipped node migration set must satisfy the invariant. This is the
    # regression guard for OMN-12942: node_projection_llm_routing's view must
    # have its base table created in-node before the view.
    assert main([]) == 0
