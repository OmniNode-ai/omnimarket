# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19029: the claims migration reconciles its own shape and delivers its grant.

Two omnibase_infra gates refuse the vendored copy of
``0001_delegate_skill_command_claims.sql``, and neither is fixable in the vendor
repo: ``node-migration-sync`` compares the vendored bytes against this source
byte for byte, so editing only the copy trades one red gate for another. Both
obligations therefore belong to this lineage, and both are asserted here rather
than only over there -- a cross-repo gate that is the sole holder of an
invariant makes every violation a round trip through two CI runs to discover.

GAP 1 -- SHAPE RECONCILIATION (OMN-15376 class)
    ``CREATE TABLE IF NOT EXISTS`` SILENTLY NO-OPS against a pre-existing table
    of the same name and a different shape. The ``CREATE INDEX IF NOT EXISTS``
    that follows guards the index NAME, not the COLUMN, so it raises
    ``column "correlation_id" does not exist``, and ``ON_ERROR_STOP=1`` kills
    the whole migration Job there. The class has cost two full deploy cycles
    already (OMN-15376 on ``llm_cost_aggregates.aggregation_key``, OMN-15302 on
    ``baselines_comparisons.snapshot_id``), one per cycle, because the runner
    halts at the first failure.

GAP 2 -- GRANT DELIVERY (OMN-17374 class)
    ``db_io.db_tables`` declares this relation ``read_write``, so the topology
    DERIVES a table grant for the ``omninode_runtime`` principal. Declaring a
    grant is not issuing one: the two halves have drifted apart five times in a
    fortnight, and the drift only ever surfaces as a live outage on whichever
    relation takes traffic next -- every write refused with
    ``InsufficientPrivilege`` while the consumer reports Stable at LAG 0 and
    commits its offsets anyway (OMN-17379).

WHY THE EXPECTATIONS ARE DERIVED, NOT LISTED. The columns come from the
``CREATE TABLE`` itself and the privilege set comes from the contract's own
declared ``access`` mode. A hand-written list of four column names would pass
unchanged the day a fifth column is added, which is the one day it matters.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
NODE_DIR = (
    REPO_ROOT / "src" / "omnimarket" / "nodes" / "node_delegate_skill_orchestrator"
)
MIGRATIONS_DIR = NODE_DIR / "migrations"
CLAIMS_MIGRATION = MIGRATIONS_DIR / "0001_delegate_skill_command_claims.sql"
GRANT_MIGRATION = (
    MIGRATIONS_DIR / "0001_grant_omninode_runtime_delegate_skill_command_claims.sql"
)
CONTRACT = NODE_DIR / "contract.yaml"

RELATION = "delegate_skill_command_claims"
SCHEMA = "omninode_internal"
QUALIFIED = f"{SCHEMA}.{RELATION}"
PRINCIPAL = "omninode_runtime"

# The markers the omnibase_infra execution proof strips to derive its RED
# variant (``test_node_migration_shape_drift_omn15376.py``). A reconciliation
# written without them is not merely undocumented: that proof asserts it found
# a region to strip, so the RED half would fail outright.
BEGIN_MARKER = "-- ---- BEGIN OMN-15376 shape reconciliation:"
END_MARKER = "-- ---- END OMN-15376 shape reconciliation:"

# Mirrors omnibase_infra ``topology/table_grant_derivation.py``: SELECT rides
# with every write set because the writer's statement is an upsert whose
# RETURNING clause reads the accepted row back.
PRIVILEGES_FOR_ACCESS = {
    "read": ("SELECT",),
    "write": ("SELECT", "INSERT", "UPDATE"),
    "read_write": ("SELECT", "INSERT", "UPDATE"),
}

_ADD_COLUMN = re.compile(
    r"ALTER\s+TABLE\s+([A-Za-z0-9_.\"]+)\s+ADD\s+COLUMN\s+IF\s+NOT\s+EXISTS\s+"
    r'("?[A-Za-z_][A-Za-z0-9_]*"?)([^;]*);',
    re.I,
)
_CONSTRAINT_HEADS = frozenset(
    {"CONSTRAINT", "PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "EXCLUDE", "LIKE"}
)


def _strip_comments(sql: str) -> str:
    """Blank out ``--`` comments, preserving offsets so spans stay comparable."""
    out = list(sql)
    for match in re.finditer(r"--[^\n]*", sql):
        for index in range(match.start(), match.end()):
            out[index] = " "
    return "".join(out)


def _claims_sql() -> str:
    assert CLAIMS_MIGRATION.is_file(), f"missing migration: {CLAIMS_MIGRATION}"
    return CLAIMS_MIGRATION.read_text(encoding="utf-8")


def _declared_columns(sql: str) -> list[str]:
    """Column names the guarded CREATE TABLE declares, in declaration order."""
    masked = _strip_comments(sql)
    match = re.search(
        r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+" + re.escape(QUALIFIED) + r"\s*\(",
        masked,
        re.I,
    )
    assert match is not None, (
        f"no guarded CREATE TABLE for {QUALIFIED} in {CLAIMS_MIGRATION.name}; "
        "this parser and the migration have diverged"
    )
    depth, start = 0, match.end() - 1
    end = len(masked)
    for index in range(start, len(masked)):
        if masked[index] == "(":
            depth += 1
        elif masked[index] == ")":
            depth -= 1
            if depth == 0:
                end = index
                break
    body, items, depth, item_start = masked[start + 1 : end], [], 0, 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(body[item_start:index])
            item_start = index + 1
    items.append(body[item_start:])

    columns: list[str] = []
    for item in items:
        head = re.match(r'\s*"?([A-Za-z_][A-Za-z0-9_]*)"?\s', item)
        if head is None or head.group(1).upper() in _CONSTRAINT_HEADS:
            continue
        columns.append(head.group(1))
    return columns


def _reconciling_adds(sql: str) -> dict[str, str]:
    """``column -> the rest of its guarded ADD COLUMN statement``."""
    masked = _strip_comments(sql)
    adds: dict[str, str] = {}
    for match in _ADD_COLUMN.finditer(masked):
        if match.group(1).split(".")[-1].strip('"').lower() != RELATION:
            continue
        adds[match.group(2).strip('"').lower()] = match.group(3)
    return adds


def _declared_access() -> str:
    contract = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))
    tables = contract["db_io"]["db_tables"]
    declared = [table for table in tables if table["name"] == RELATION]
    assert len(declared) == 1, (
        f"expected exactly one db_io declaration for {RELATION}, got {declared}"
    )
    assert declared[0]["schema"] == SCHEMA, declared[0]
    return str(declared[0]["access"])


def test_the_migration_declares_the_columns_this_suite_reasons_about() -> None:
    """Anti-vacuity: an unparsed CREATE TABLE would make every gap test pass."""
    columns = _declared_columns(_claims_sql())
    assert columns == [
        "delivery_id",
        "correlation_id",
        "claimed_at",
        "terminal_json",
    ], columns


def test_every_declared_column_is_reconciled_by_a_guarded_add() -> None:
    """The OMN-15376 obligation: converge a drifted table, never no-op past it."""
    sql = _claims_sql()
    covered = set(_reconciling_adds(sql))
    gaps = [
        column for column in _declared_columns(sql) if column.lower() not in covered
    ]
    assert not gaps, (
        f"{CLAIMS_MIGRATION.name}: CREATE TABLE IF NOT EXISTS no-ops against a "
        f"drifted pre-existing table and these declared columns are never "
        f"reconciled, so the next column-dependent statement kills the deploy "
        f"(OMN-15376 class): {gaps}. Add one "
        f"'ALTER TABLE {QUALIFIED} ADD COLUMN IF NOT EXISTS <col> <type> "
        f"[DEFAULT ...];' per declared column, immediately after the CREATE TABLE."
    )


def test_the_guarded_adds_are_nullable() -> None:
    """OMN-16777: a NOT NULL add cannot reconcile a drifted table holding rows.

    The add runs against a table whose row count is unknown. ``NOT NULL``
    without a default is refused outright on a non-empty table, and with one it
    invents data for rows that never had the column -- so the guarded form is
    nullable, and the writer, not the schema, is what keeps the values present.
    """
    offenders = {
        column: clause
        for column, clause in _reconciling_adds(_claims_sql()).items()
        if re.search(r"\bNOT\s+NULL\b", clause, re.I)
    }
    assert not offenders, (
        f"guarded ADD COLUMN must be nullable on the drifted path: {offenders}"
    )


def test_the_reconciliation_is_delimited_by_the_markers_the_proof_strips() -> None:
    """Without the markers the omnibase_infra execution proof has no RED half."""
    sql = _claims_sql()
    unmarked = (
        "the reconciliation must be wrapped in the "
        f"'{BEGIN_MARKER} <table> ----' / '{END_MARKER} <table> ----' markers: "
        "tests/integration/migrations/test_node_migration_shape_drift_omn15376.py "
        "derives its pre-fix variant by deleting exactly that region and asserts "
        "it found one, so an unmarked fix fails the proof rather than passing it."
    )
    assert BEGIN_MARKER in sql, unmarked
    assert END_MARKER in sql, unmarked
    begin = sql.index(BEGIN_MARKER)
    end = sql.index(END_MARKER)
    assert begin < end, "reconciliation markers are inverted"
    block = _strip_comments(sql[begin:end]).upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "TRUNCATE", "DELETE FROM"):
        assert forbidden not in block, (
            f"'{forbidden}' inside the reconciliation block, which runs against "
            "tables whose row count is unknown: converge the shape, never "
            "destroy the data."
        )


def test_the_reconciliation_precedes_the_first_column_dependent_statement() -> None:
    """Order is the whole defect: the index on correlation_id is what dies."""
    masked = _strip_comments(_claims_sql())
    index_statement = re.search(
        r"CREATE\s+INDEX\s+IF\s+NOT\s+EXISTS[^;]*\(\s*correlation_id\s*\)", masked, re.I
    )
    assert index_statement is not None, (
        "the correlation_id index is gone; this test names the statement that "
        "actually failed in the OMN-15376 class and must be retargeted, not deleted"
    )
    add = next(
        (
            match
            for match in _ADD_COLUMN.finditer(masked)
            if match.group(2).strip('"').lower() == "correlation_id"
        ),
        None,
    )
    assert add is not None, "correlation_id has no guarded ADD COLUMN at all"
    assert add.end() < index_statement.start(), (
        "the guarded ADD COLUMN for correlation_id runs AFTER the index that "
        "depends on it, so the drifted path still dies on "
        "'column \"correlation_id\" does not exist' before reaching it."
    )


def test_a_companion_migration_delivers_the_grant_the_contract_derives() -> None:
    """A declared grant with no delivering migration is an outage in waiting."""
    assert GRANT_MIGRATION.is_file(), (
        f"{RELATION} is declared '{_declared_access()}' in {CONTRACT.name}, so the "
        f"topology derives a TABLE grant for {PRINCIPAL} that no migration issues. "
        f"omnibase_infra's grant-delivery ratchet refuses it: 'UNDELIVERED "
        f"{PRINCIPAL} -> {QUALIFIED}'. Add {GRANT_MIGRATION.name} in this node's "
        "own lineage, in the shape 0001_grant_omninode_runtime_lab_lane_health.sql "
        "and 0001_grant_omninode_runtime_dod_verify_runs.sql already use."
    )
    sql = _strip_comments(GRANT_MIGRATION.read_text(encoding="utf-8"))
    assert re.search(
        r"GRANT\s+USAGE\s+ON\s+SCHEMA\s+" + SCHEMA + r"\s+TO\s+" + PRINCIPAL, sql, re.I
    ), (
        "the schema USAGE grant is missing. It mirrors the topology's SCHEMA "
        "grant and is re-asserted here because a migration must not depend on a "
        "sibling file having run."
    )
    expected = PRIVILEGES_FOR_ACCESS[_declared_access()]
    table_grant = re.search(
        r"GRANT\s+([A-Z,\s]+?)\s+ON\s+"
        + re.escape(QUALIFIED)
        + r"\s+TO\s+"
        + PRINCIPAL,
        sql,
        re.I,
    )
    assert table_grant is not None, f"no TABLE grant on {QUALIFIED} to {PRINCIPAL}"
    granted = {word.strip().upper() for word in table_grant.group(1).split(",")}
    assert granted == set(expected), (
        f"the delivered privileges must equal the set the contract's "
        f"access='{_declared_access()}' derives {sorted(expected)}; found {sorted(granted)}. "
        "A narrower set refuses writes on a lane nobody rebuilt; a wider one "
        "grants a privilege the declaration does not justify."
    )


def test_the_grant_migration_asserts_every_privilege_it_grants() -> None:
    """OMN-17379 shipped because only INSERT was asserted, and it was true."""
    sql = _strip_comments(GRANT_MIGRATION.read_text(encoding="utf-8"))
    for privilege in PRIVILEGES_FOR_ACCESS[_declared_access()]:
        assert re.search(r"privilege_type\s*=\s*'" + privilege + r"'", sql, re.I), (
            f"{privilege} is granted but never asserted. A grant that did not "
            "take must fail the migration loudly, not wait for the relation to "
            "take traffic: pr_merged_events sat 24 days behind its topic because "
            "the one privilege that was asserted was the one that was present."
        )
    assert "1 / count(*)" in sql, (
        "assertions must use the fail-loud division-by-zero shape the other "
        "grant migrations in this repository use"
    )


def test_the_grant_migration_claims_nothing_the_relation_cannot_back() -> None:
    """No DELETE, and no sequence grant: this key is TEXT, not BIGSERIAL."""
    sql = _strip_comments(GRANT_MIGRATION.read_text(encoding="utf-8")).upper()
    for forbidden in ("DELETE", "TRUNCATE", "ALL PRIVILEGES", "GRANT ALL"):
        assert forbidden not in sql, (
            f"'{forbidden}' is granted but nothing in this node's declaration "
            "justifies it; the claim is never retracted and the row is history."
        )
    assert "ON SEQUENCE" not in sql, (
        "the sequence half (OMN-17447) exists for a BIGSERIAL key whose "
        "standalone sequence carries its own acl. This key is a TEXT delivery "
        "id, so there is no sequence to grant on and naming one would fail the "
        "migration on an object that does not exist."
    )


def test_the_grant_file_sorts_after_the_relation_it_grants_on() -> None:
    """The runner applies a node's migrations in filename order."""
    names = sorted(path.name for path in MIGRATIONS_DIR.glob("*.sql"))
    assert names.index(CLAIMS_MIGRATION.name) < names.index(GRANT_MIGRATION.name), (
        "the grant would be applied before the relation exists"
    )
