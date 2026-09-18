# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18774 AC4/AC5: the enumeration, and the ratchet that keeps it empty.

AC4 asked for the blast radius ENUMERATED rather than sampled: every relation
whose owning contract declares ``omninode_internal`` or ``platform_catalog``
and which carries a policy predicated on a session GUC. The enumeration is
produced by joining the contract declarations against the node migration tree
(``scripts/validation/check_internal_relation_tenant_posture.py``) and it is
asserted here to be EMPTY. The two relations the diagnosis named --
``generation_events`` and ``node_service_registry`` -- are the two the join
found independently, and the migrations that land with this file are what
empty it.

AC5 asked for a GATE, not a detector (Operating Rule 5): this is the fourth
distinct RLS-shape defect on this family (OMN-17288 -> OMN-17298 -> OMN-17315
-> this), and detection that is not wired pre-merge does not hold. The same
computation serves both, so the enumeration cannot go stale relative to the
gate -- there is only one of them.

WHY THE FINAL POSTURE AND NOT THE DIFF. A diff-scoped gate answers "did this
change add the shape", which leaves every already-landed instance invisible
and needs a base ref to run at all. The final posture answers "does the tree
express the shape", which is the question both criteria ask.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from omnibase_core.enums.enum_database_schema_domain import EnumDatabaseSchemaDomain

from omnimarket.projection.relation_domains import (
    SCHEMA_DOMAINS,
    declared_relation_domain,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE = (
    REPO_ROOT / "scripts" / "validation" / "check_internal_relation_tenant_posture.py"
)
NODES_DIR = REPO_ROOT / "src" / "omnimarket" / "nodes"

sys.path.insert(0, str(GATE.parent))

from check_internal_relation_tenant_posture import (  # noqa: E402
    evaluate,
    replay_migrations,
    strip_sql_comments,
)


def _run_gate(nodes_dir: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GATE), "--nodes-dir", str(nodes_dir)],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_node(
    nodes_dir: Path, node: str, relation: str, schema: str, migration_sql: str
) -> None:
    node_dir = nodes_dir / node
    (node_dir / "migrations").mkdir(parents=True)
    (node_dir / "contract.yaml").write_text(
        "name: "
        + node
        + "\ndb_io:\n  db_tables:\n    - name: "
        + relation
        + "\n      database_ref: application\n      schema: "
        + schema
        + '\n      migration: "0001_x.sql"\n      access: write\n      role: r\n',
        encoding="utf-8",
    )
    (node_dir / "migrations" / "0001_x.sql").write_text(migration_sql, encoding="utf-8")


_GUC_MIGRATION = """
CREATE TABLE IF NOT EXISTS {relation} (id uuid primary key);
ALTER TABLE {relation} ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT 'omninode';
ALTER TABLE {relation} ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON {relation}
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
"""


class TestTheEnumeration:
    """AC4: the blast radius, joined rather than sampled."""

    def test_no_internal_or_catalog_relation_carries_a_tenant_posture(self) -> None:
        findings, conflicted, declarations = evaluate(NODES_DIR)
        assert conflicted == [], conflicted
        assert findings == [], "\n".join(finding.render() for finding in findings)
        assert declarations, "the join found no declarations at all -- it is broken"

    def test_the_two_named_relations_are_in_the_enumeration_domain(self) -> None:
        """The pair the diagnosis named, classified by the join itself."""
        for relation in ("generation_events", "node_service_registry"):
            assert (
                declared_relation_domain(relation)
                is EnumDatabaseSchemaDomain.OMNINODE_INTERNAL
            )

    def test_the_migration_tree_expresses_the_chosen_end_state(self) -> None:
        """AC1: contracts and migration tree must express the SAME end state."""
        postures = replay_migrations(
            sorted(
                NODES_DIR.glob("*/migrations/*.sql"),
                key=lambda path: (path.parent.parent.name, path.name),
            )
        )
        for relation in ("generation_events", "node_service_registry"):
            posture = postures[relation]
            assert posture.rls_enabled is False, relation
            assert posture.guc_policies == {}, relation
            assert posture.tenant_id_column is False, relation


class TestThePositiveControl:
    """A tenant-declared relation keeps everything the internal ones shed."""

    def test_a_tenant_declared_relation_keeps_its_guc_policy(self) -> None:
        postures = replay_migrations(
            sorted(
                NODES_DIR.glob("*/migrations/*.sql"),
                key=lambda path: (path.parent.parent.name, path.name),
            )
        )
        assert (
            declared_relation_domain("delegation_events")
            is EnumDatabaseSchemaDomain.TENANT
        )
        posture = postures["delegation_events"]
        assert posture.rls_enabled is True
        assert "tenant_isolation" in posture.guc_policies
        assert posture.tenant_id_column is True

    def test_the_live_tree_passes_the_gate(self) -> None:
        result = _run_gate(NODES_DIR)
        assert result.returncode == 0, result.stderr


class TestTheRatchet:
    """AC5: a NEW migration with the shape is refused, fail-closed."""

    def test_an_internal_declared_relation_with_rls_is_refused(
        self, tmp_path: Path
    ) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic_internal",
            "synthetic_internal_rows",
            "omninode_internal",
            _GUC_MIGRATION.format(relation="synthetic_internal_rows"),
        )
        result = _run_gate(nodes)
        assert result.returncode == 1, result.stdout
        assert "synthetic_internal_rows" in result.stderr
        assert "row-level security is ENABLEd" in result.stderr

    def test_a_platform_catalog_relation_with_rls_is_refused(
        self, tmp_path: Path
    ) -> None:
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic_catalog",
            "synthetic_catalog_rows",
            "platform_catalog",
            _GUC_MIGRATION.format(relation="synthetic_catalog_rows"),
        )
        assert _run_gate(nodes).returncode == 1

    def test_the_same_migration_on_a_tenant_relation_passes(
        self, tmp_path: Path
    ) -> None:
        """The positive control the criterion names, byte-identical DDL."""
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic_tenant",
            "synthetic_tenant_rows",
            "tenant",
            _GUC_MIGRATION.format(relation="synthetic_tenant_rows"),
        )
        result = _run_gate(nodes)
        assert result.returncode == 0, result.stderr

    def test_a_later_migration_dropping_the_posture_clears_the_refusal(
        self, tmp_path: Path
    ) -> None:
        """The remedy the gate names must actually clear it."""
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic_internal",
            "synthetic_internal_rows",
            "omninode_internal",
            _GUC_MIGRATION.format(relation="synthetic_internal_rows"),
        )
        (nodes / "node_synthetic_internal" / "migrations" / "0002_drop.sql").write_text(
            "DROP POLICY IF EXISTS tenant_isolation ON synthetic_internal_rows;\n"
            "ALTER TABLE synthetic_internal_rows DISABLE ROW LEVEL SECURITY;\n"
            "ALTER TABLE synthetic_internal_rows DROP COLUMN IF EXISTS tenant_id;\n",
            encoding="utf-8",
        )
        assert _run_gate(nodes).returncode == 0


class TestTheCommentTrap:
    """A migration that DESCRIBES a statement must not read as issuing it.

    ``node_projection_registration/0004_node_service_registry_no_force_rls.sql``
    says in prose that it is "narrower than DISABLE ROW LEVEL SECURITY or DROP
    POLICY". A scan over raw bytes reads that sentence as both statements and
    reports the relation clean -- the exact false negative this gate exists to
    avoid, produced by the gate's own input.
    """

    def test_prose_naming_the_statements_is_not_read_as_the_statements(self) -> None:
        sql = (
            "-- narrower than DISABLE ROW LEVEL SECURITY or DROP POLICY\n"
            "/* also not DROP POLICY tenant_isolation ON r; */\n"
            "ALTER TABLE r ENABLE ROW LEVEL SECURITY;\n"
            "CREATE POLICY tenant_isolation ON r FOR ALL "
            "USING (tenant_id = current_setting('app.tenant_id', true));\n"
        )
        stripped = strip_sql_comments(sql)
        assert "DISABLE ROW LEVEL SECURITY" not in stripped
        assert "DROP POLICY" not in stripped

    def test_the_real_0004_does_not_read_as_a_disable(self) -> None:
        path = (
            NODES_DIR
            / "node_projection_registration"
            / "migrations"
            / "0004_node_service_registry_no_force_rls.sql"
        )
        postures = replay_migrations([path])
        # 0004 issues only NO FORCE; on its own it neither enables nor
        # disables, and it drops no policy.
        assert postures.get("node_service_registry", None) is None or (
            postures["node_service_registry"].guc_policies == {}
        )


class TestTheDomainMappingIsPinned:
    """The gate's classification source must not drift from the topology's."""

    def test_schema_domains_match_the_topology_module(self) -> None:
        from omnibase_infra.topology.application_database import (  # type: ignore[attr-defined]
            _EXPECTED_SCHEMAS,
        )

        assert dict(SCHEMA_DOMAINS) == dict(_EXPECTED_SCHEMAS), (
            "a schema classified in omnibase_infra's topology but not here is "
            "a relation this gate silently declines to judge"
        )
