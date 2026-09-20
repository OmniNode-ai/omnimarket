# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The catalog ownership declaration for dod_verify_runs (OMN-18900).

WHY A TEST FOR TWO YAML ENTRIES
    The entries are not documentation. `check_application_database_sql.py`
    (the OMN-15361 SQL ownership gate) resolves every application relation a
    migration creates or grants on against THIS file, at omnimarket's dev tip,
    from inside omnibase_infra. Delete either entry and the omnibase_infra
    pull request carrying the migration fails with "requires exactly one
    ownership declaration" -- which is exactly what happened to
    omnibase_infra#3883 before this declaration existed.

    So the failure mode is a cross-repository one: the thing that breaks is in
    another repository, on another pull request, and nothing in THIS
    repository would otherwise notice. That is what this test is for.

WHY THE SEQUENCE IS ASSERTED SEPARATELY
    A BIGSERIAL column's sequence is a standalone object carrying its own
    access control list, which PostgreSQL checks separately from the table's.
    The node's grant migration therefore grants usage on it by literal name
    and asserts that grant, and the ownership gate wants an exact physical
    identity to resolve that target against. Declaring only the table leaves
    the gate failing on the sequence -- measured, not assumed: the gate went
    from two findings to one to none as each entry was added.

    Asserting them in one test would let a future edit drop the sequence entry
    and still fail for the table's reason, which reads as the wrong defect.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.unit

MANIFEST = (
    Path(__file__).resolve().parents[1] / "scripts/application-relation-ownership.yaml"
)

TABLE = "dod_verify_runs"
SEQUENCE = "dod_verify_runs_projection_cursor_seq"
SCHEMA = "omninode_internal"
CREATE_MIGRATION = (
    "src/omnimarket/nodes/node_projection_dod_verdict/migrations/"
    "0000_create_dod_verify_runs.sql"
)


def _manifest() -> dict[str, Any]:
    with open(MANIFEST) as handle:
        return dict(yaml.safe_load(handle))


def _tables() -> list[dict[str, Any]]:
    return list(_manifest()["db_io"]["db_tables"])


def _objects() -> list[dict[str, Any]]:
    return list(_manifest()["database_objects"])


def test_the_table_is_declared_exactly_once() -> None:
    """One declaration, not zero and not two.

    The gate's own wording is "requires exactly one ownership declaration":
    two would be as fatal as none, and a duplicate is the easy mistake when a
    second lane adds the same relation from the other end.
    """
    matches = [row for row in _tables() if row["name"] == TABLE]
    assert len(matches) == 1, f"expected exactly one {TABLE} declaration, got {matches}"


def test_the_table_declaration_names_its_schema_and_its_create_migration() -> None:
    """The gate resolves the created object by schema and name, then the file.

    A declaration pointing at the wrong migration resolves the ownership and
    then fails to bind the DDL, which reads as a different defect entirely.
    """
    row = next(r for r in _tables() if r["name"] == TABLE)
    assert row["schema"] == SCHEMA
    assert row["database_ref"] == "application"
    assert row["migration"] == CREATE_MIGRATION
    assert row["access"] == "read_write"


def test_the_cursor_sequence_is_declared_as_a_sequence_object() -> None:
    """The half whose absence is invisible to a table-only reading.

    Declared under ``database_objects`` with ``kind: sequence`` rather than as
    a table row, because it is a physical catalog identity the grant migration
    targets by literal name, not a relation any node reads or writes.
    """
    matches = [row for row in _objects() if row["name"] == SEQUENCE]
    assert len(matches) == 1, f"expected exactly one {SEQUENCE} object, got {matches}"
    row = matches[0]
    assert row["kind"] == "sequence"
    assert row["schema"] == SCHEMA
    assert row["domain"] == "OMNINODE_INTERNAL"


def test_the_sequence_lives_in_the_same_schema_as_its_table() -> None:
    """Net-new in omninode_internal, so no OMN-15359 cutover entry is owed.

    Several older sequences in this file sit in ``public`` because their
    owning tables have not moved yet. This pair never lived there, and a
    declaration claiming ``public`` would send the gate looking for an object
    that does not exist under that name.
    """
    table = next(r for r in _tables() if r["name"] == TABLE)
    sequence = next(r for r in _objects() if r["name"] == SEQUENCE)
    assert sequence["schema"] == table["schema"] == SCHEMA


def test_the_sequence_name_is_the_one_postgres_will_actually_mint() -> None:
    """``<table>_<column>_seq`` is not a convention here, it is the identity.

    PostgreSQL derives a BIGSERIAL sequence's name from the table and column,
    the grant migration spells that name literally, and the migration's own
    first assertion fails the run unless ``pg_get_serial_sequence`` resolves
    the column to exactly it. A declaration under any other spelling would
    satisfy this file and none of those three.
    """
    assert f"{TABLE}_projection_cursor_seq" == SEQUENCE
    assert any(row["name"] == SEQUENCE for row in _objects())
