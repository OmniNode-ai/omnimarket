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

import re
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


class TestTheParserHoldsUnderRealSqlShapes:
    """Two shapes an adversarial review found, both pinned rather than argued.

    Neither is hypothetical: the first changes what the gate reads as code,
    and the second changes whether it sees a column at all. A gate that is
    wrong about its own input is worse than no gate, because its silence
    reads as a clean bill of health.
    """

    def test_a_nested_block_comment_is_blanked_to_its_last_terminator(
        self,
    ) -> None:
        """PostgreSQL block comments NEST; a non-greedy match does not.

        A non-greedy scan ends the comment at the inner terminator and hands
        the rest of the commented prose back as code -- so a migration that
        explains itself inside a nested comment would be read as issuing the
        statements it merely describes.
        """
        sql = (
            "/* outer /* inner */ ALTER TABLE r DISABLE ROW LEVEL SECURITY; */\n"
            "ALTER TABLE r ENABLE ROW LEVEL SECURITY;\n"
        )
        stripped = strip_sql_comments(sql)
        assert "DISABLE ROW LEVEL SECURITY" not in stripped
        assert "ENABLE ROW LEVEL SECURITY" in stripped

    def test_blanking_preserves_every_offset(self) -> None:
        """Offsets index the original bytes, so event ordering stays true."""
        sql = "/* a /* b */ c */SELECT 1;"
        assert len(strip_sql_comments(sql)) == len(sql)

    def test_a_tenant_id_after_a_parenthesised_column_is_still_found(
        self, tmp_path: Path
    ) -> None:
        """Terminating on the first `);` truncates the column list.

        ``NUMERIC(18, 6)``, ``CHECK (n > 0)`` and ``DEFAULT gen_random_uuid()``
        all close a parenthesis of their own, and generation_events carries
        two of the three. A tenant_id declared after one of them must still
        be seen.
        """
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic_inline",
            "synthetic_inline_rows",
            "omninode_internal",
            "CREATE TABLE IF NOT EXISTS synthetic_inline_rows (\n"
            "  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),\n"
            "  cost NUMERIC(18, 6) NOT NULL DEFAULT 0,\n"
            "  attempts INT NOT NULL DEFAULT 0 CHECK (attempts >= 0),\n"
            "  tenant_id TEXT NOT NULL DEFAULT 'omninode'\n"
            ");\n",
        )
        result = _run_gate(nodes)
        assert result.returncode == 1, result.stdout
        assert "carries a tenant_id column" in result.stderr

    def test_the_same_shape_on_a_tenant_relation_passes(self, tmp_path: Path) -> None:
        """The positive control for the paren-balancing arm."""
        nodes = tmp_path / "nodes"
        _write_node(
            nodes,
            "node_synthetic_inline_tenant",
            "synthetic_inline_tenant_rows",
            "tenant",
            "CREATE TABLE IF NOT EXISTS synthetic_inline_tenant_rows (\n"
            "  cost NUMERIC(18, 6) NOT NULL DEFAULT 0,\n"
            "  tenant_id TEXT NOT NULL\n"
            ");\n",
        )
        assert _run_gate(nodes).returncode == 0


class TestTheWriterAssertionNamesItsOwnFailure:
    """A re-classification must fail loudly at the writer, with its own type."""

    def test_a_tenant_reclassification_raises_the_mismatch_error(self) -> None:
        from omnimarket.projection.relation_domains import (
            RelationDomainMismatchError,
            assert_internal_relation,
        )

        assert_internal_relation("generation_events")
        with pytest.raises(RelationDomainMismatchError, match="delegation_events"):
            assert_internal_relation("delegation_events")

    def test_an_undeclared_relation_is_refused_rather_than_guessed(self) -> None:
        from omnimarket.projection.relation_domains import (
            UndeclaredRelationError,
            declared_relation_domain,
        )

        with pytest.raises(UndeclaredRelationError, match="no node contract"):
            declared_relation_domain("relation_no_contract_declares")


class TestNoWriterDependsOnTheDroppedColumn:
    """The precondition the two migrations rest on, checked rather than asserted.

    A migration that drops a column is only safe if nothing still writes or
    reads it. Both migrations say so in prose; this is the mechanical half,
    because prose in a migration is a claim and not a proof.

    Three independent things have to hold, and each is checked here:

    * no SQL statement anywhere in this package names one of the two relations
      AND ``tenant_id``;
    * the contract-driven stamp returns nothing for them, so no handler row
      dict can carry the key; and
    * the two relations really are the ones the migrations name, read from the
      migration bytes rather than from a list here.

    The database enforces the fourth: ``DROP COLUMN`` without ``CASCADE``
    refuses outright if a view, index or constraint still depends on the
    column, and the live readback on the lane found no dependent view.

    BOUND, stated rather than implied: this reads SQL that appears as a
    literal in the source. A statement assembled entirely at runtime from
    values this scan cannot see is outside it. That is the same bound every
    source-level ratchet in this repo carries, and the kernel refusal is the
    backstop underneath it -- ``InternalProjectionTableOperation`` raises on a
    ``tenant_id`` key whatever assembled the row.
    """

    RELATIONS = ("generation_events", "node_service_registry")

    def _statements_naming(self, relation: str) -> list[tuple[Path, str]]:
        """Every SQL-ish literal in the package that names ``relation``."""
        found: list[tuple[Path, str]] = []
        package = REPO_ROOT / "src" / "omnimarket"
        for path in sorted(package.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if relation not in text:
                continue
            for statement in re.split(r";|\"\"\"|'''", text):
                if relation in statement and re.search(
                    r"\b(INSERT\s+INTO|UPDATE|SELECT|DELETE\s+FROM)\b",
                    statement,
                    re.IGNORECASE,
                ):
                    found.append((path, statement))
        return found

    def test_no_statement_names_the_relation_and_the_dropped_column(self) -> None:
        offenders: list[str] = []
        for relation in self.RELATIONS:
            for path, statement in self._statements_naming(relation):
                if re.search(r"\btenant_id\b", statement):
                    offenders.append(f"{path.relative_to(REPO_ROOT)} -> {relation}")
        assert offenders == [], (
            "a statement still names a relation whose tenant_id column is "
            f"dropped by OMN-18774: {offenders}"
        )

    def test_the_scan_can_actually_find_a_statement(self) -> None:
        """Positive control: an empty offender list must not be vacuous."""
        assert self._statements_naming("generation_events"), (
            "the scan found no statement naming generation_events at all, so "
            "its clean result above proves nothing"
        )

    def test_the_contract_driven_stamp_supplies_no_key_for_either(self) -> None:
        from omnimarket.projection.relation_domains import tenant_write_stamp

        for relation in self.RELATIONS:
            assert tenant_write_stamp(table=relation) == {}

    def test_the_migrations_name_exactly_these_two_relations(self) -> None:
        """Read from the migration bytes, so the pair above cannot drift."""
        migrations = {
            "generation_events": NODES_DIR
            / "node_projection_delegation"
            / "migrations"
            / "0043_generation_events_drop_tenant_posture.sql",
            "node_service_registry": NODES_DIR
            / "node_projection_registration"
            / "migrations"
            / "0007_node_service_registry_drop_tenant_posture.sql",
        }
        for relation, path in migrations.items():
            body = " ".join(path.read_text(encoding="utf-8").split())
            assert "DROP COLUMN IF EXISTS tenant_id" in body, relation
            assert relation in body
            assert "CASCADE" not in body, (
                f"{relation}: the drop must stay non-CASCADE so PostgreSQL "
                "refuses rather than silently removing a dependent object"
            )
