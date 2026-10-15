# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17298: a relation that ends the migration tree with row-level security
ENABLEd and NO policy at all is refused.

A table with RLS enabled and zero policies denies every non-owner statement,
and with ``FORCE ROW LEVEL SECURITY`` it denies the owner too. The projection
writer's insert fails ``InsufficientPrivilege`` and the message goes to the
DLQ, so the outcome is durable nowhere. This is the shape OMN-17288 found in
migration 0032 and OMN-17298 found on a second tenant projection relation;
the audit this file pins asks the question over the whole node migration tree,
tenant- and internal-declared relations alike, and answers it from the final
posture so the answer cannot go stale against the gate.

The positive control matters as much as the refusal: an audit that reports
zero relations because it parsed nothing would pass vacuously, so the live-tree
test also asserts the tree holds RLS-enabled relations at all.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = (
    REPO_ROOT / "scripts" / "validation" / "check_internal_relation_tenant_posture.py"
)
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"


def _load_gate() -> ModuleType:
    spec = importlib.util.spec_from_file_location("omn17298_posture_gate", GATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_gate = _load_gate()
node_migration_paths = _gate.node_migration_paths
replay_migrations = _gate.replay_migrations
rls_without_policy = _gate.rls_without_policy


def _run_gate(nodes_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--nodes-dir", str(nodes_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_node(nodes_dir: Path, node: str, relation: str, *migrations: str) -> None:
    node_dir = nodes_dir / node
    (node_dir / "migrations").mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        "name: "
        + node
        + "\ndb_io:\n  db_tables:\n    - name: "
        + relation
        + "\n      database_ref: application\n      schema: tenant"
        + '\n      migration: "0001_x.sql"\n      access: write\n      role: r\n',
        encoding="utf-8",
    )
    for index, sql in enumerate(migrations, start=1):
        (node_dir / "migrations" / f"{index:04d}_x.sql").write_text(
            sql, encoding="utf-8"
        )


_ENABLE_ONLY = """
CREATE TABLE IF NOT EXISTS {relation} (id uuid primary key, tenant_id TEXT NOT NULL);
ALTER TABLE {relation} ENABLE ROW LEVEL SECURITY;
ALTER TABLE {relation} FORCE ROW LEVEL SECURITY;
"""

_POLICY = """
CREATE POLICY tenant_isolation ON {relation}
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
"""

_NON_GUC_POLICY = """
CREATE POLICY writer_all ON {relation} FOR ALL TO tenant_projection_writer
  USING (true) WITH CHECK (true);
"""


class TestTheLiveTree:
    def test_no_relation_ends_the_tree_rls_enabled_with_zero_policies(self) -> None:
        postures = replay_migrations(node_migration_paths(NODES_DIR))
        assert rls_without_policy(postures) == []

    def test_the_audit_is_not_vacuous(self) -> None:
        """Positive control: the replay really sees RLS-enabled relations, so a
        zero above is a finding about the tree and not about the parser."""
        postures = replay_migrations(node_migration_paths(NODES_DIR))
        enabled = {name for name, p in postures.items() if p.rls_enabled}
        assert "projection_delegation_inference_response_text" in enabled
        assert "delegation_events" in enabled
        assert all(postures[name].policies for name in enabled)

    def test_the_live_tree_passes_the_gate(self) -> None:
        result = _run_gate(NODES_DIR)
        assert result.returncode == 0, result.stderr


class TestTheRefusal:
    def test_enable_and_force_with_no_policy_is_refused(self, tmp_path: Path) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            _ENABLE_ONLY.format(relation="synthetic_rows"),
        )
        result = _run_gate(nodes)
        assert result.returncode == 1, result.stdout
        assert "synthetic_rows" in result.stderr
        assert "zero policies" in result.stderr

    def test_a_relation_no_contract_declares_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "declared_rows",
            "CREATE TABLE declared_rows (id int);\n",
            _ENABLE_ONLY.format(relation="undeclared_rows"),
        )
        result = _run_gate(nodes)
        assert result.returncode == 1, result.stdout
        assert "undeclared_rows" in result.stderr

    def test_a_policy_dropped_by_a_later_migration_is_refused(
        self, tmp_path: Path
    ) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            _ENABLE_ONLY.format(relation="synthetic_rows")
            + _POLICY.format(relation="synthetic_rows"),
            "DROP POLICY IF EXISTS tenant_isolation ON synthetic_rows;\n",
        )
        result = _run_gate(nodes)
        assert result.returncode == 1, result.stdout
        assert "synthetic_rows" in result.stderr

    def test_a_drop_then_create_in_one_migration_is_not_a_window(
        self, tmp_path: Path
    ) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            _ENABLE_ONLY.format(relation="synthetic_rows")
            + _POLICY.format(relation="synthetic_rows"),
            "DROP POLICY IF EXISTS tenant_isolation ON synthetic_rows;\n"
            + _POLICY.format(relation="synthetic_rows"),
        )
        assert _run_gate(nodes).returncode == 0


class TestThePositiveControls:
    def test_enable_with_a_guc_policy_passes(self, tmp_path: Path) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            _ENABLE_ONLY.format(relation="synthetic_rows")
            + _POLICY.format(relation="synthetic_rows"),
        )
        result = _run_gate(nodes)
        assert result.returncode == 0, result.stderr

    def test_enable_with_a_non_guc_policy_passes(self, tmp_path: Path) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            _ENABLE_ONLY.format(relation="synthetic_rows")
            + _NON_GUC_POLICY.format(relation="synthetic_rows"),
        )
        result = _run_gate(nodes)
        assert result.returncode == 0, result.stderr

    def test_rls_disabled_again_passes(self, tmp_path: Path) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            _ENABLE_ONLY.format(relation="synthetic_rows"),
            "ALTER TABLE synthetic_rows DISABLE ROW LEVEL SECURITY;\n",
        )
        result = _run_gate(nodes)
        assert result.returncode == 0, result.stderr

    def test_prose_naming_the_statements_is_not_read_as_the_statements(
        self, tmp_path: Path
    ) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic",
            "synthetic_rows",
            "-- ALTER TABLE synthetic_rows ENABLE ROW LEVEL SECURITY;\n"
            "CREATE TABLE synthetic_rows (id int);\n",
        )
        result = _run_gate(nodes)
        assert result.returncode == 0, result.stderr
