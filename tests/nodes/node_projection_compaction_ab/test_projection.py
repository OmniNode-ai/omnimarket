# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1: one writer per column, one row per key, no backwards moves."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from omnibase_infra.runtime.auto_wiring.discovery import discover_contracts_from_paths
from omnibase_infra.validation.application_database_domain_enforcement import (
    load_application_database_ownership_identities,
)
from pydantic import ValidationError

from omnimarket.nodes.node_projection_compaction_ab.handlers.handler_projection_compaction_ab import (
    InMemoryAbStore,
    upsert_sql,
)
from omnimarket.nodes.node_projection_compaction_ab.models import (
    EnumAbWriter,
    ModelAbEvent,
    ModelAbRow,
    load_live_spec,
)

T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)
NODE = Path(__file__).parents[3] / "src/omnimarket/nodes/node_projection_compaction_ab"


def test_migration_ownership_without_runtime_database_access() -> None:
    manifest = discover_contracts_from_paths([NODE / "contract.yaml"])
    assert not manifest.errors, manifest.errors
    assert len(manifest.contracts) == 1
    assert manifest.contracts[0].db_io is None
    ownership = load_application_database_ownership_identities(
        [NODE.parents[3] / "scripts/application-relation-ownership.yaml"]
    )
    assert {
        (row.schema, row.name, row.kind.value)
        for row in ownership
        if row.name.startswith("compaction_ab_")
    } == {
        ("omninode_internal", "compaction_ab_rows", "table"),
        ("omninode_internal", "compaction_ab_build_series", "table"),
    }


def _event(
    writer: EnumAbWriter,
    values: dict[str, str | int | bool],
    seconds: int,
    eid: str,
    session: str = "s1",
) -> ModelAbEvent:
    return ModelAbEvent(
        experiment_id="e1",
        session_id=session,
        boundary_uuid="b1",
        writer=writer,
        event_time=T0 + timedelta(seconds=seconds),
        event_id=eid,
        values=values,
    )


def test_upsert_key_one_row_per_key() -> None:
    store = InMemoryAbStore()
    store.apply(_event(EnumAbWriter.ASSIGNMENT, {"arm": "on"}, 1, "a"))
    store.apply(_event(EnumAbWriter.SCORES, {"hl1_events": 2}, 2, "b"))
    store.apply(_event(EnumAbWriter.COVARIATES, {"trigger": "auto"}, 3, "c"))
    store.apply(_event(EnumAbWriter.ASSIGNMENT, {"arm": "off"}, 4, "d", session="s2"))
    rows = store.rows()
    assert set(rows) == {("e1", "s1", "b1"), ("e1", "s2", "b1")}
    merged = rows[("e1", "s1", "b1")]
    assert (merged.arm, merged.hl1_events, merged.trigger) == ("on", 2, "auto")


def test_upsert_key_migration_and_single_writer() -> None:
    spec = load_live_spec()
    assert spec.key_columns == ("experiment_id", "session_id", "boundary_uuid")
    sql = (NODE / "migrations/0000_create_compaction_ab.sql").read_text()
    tables = dict(
        re.findall(
            r"CREATE TABLE IF NOT EXISTS omninode_internal\.(\w+) \((.*?)\n\);",
            sql,
            re.S,
        )
    )
    body = tables["compaction_ab_rows"]
    columns = re.findall(r"^\s{4}(?!PRIMARY)(\w+)\s+[A-Z]", body, re.M)
    assert len(columns) == len(set(columns)), "duplicate column in migration"
    assert re.search(r"PRIMARY KEY \(experiment_id, session_id, boundary_uuid\)", body)
    assert sql.count("PRIMARY KEY") == 2
    owners: dict[str, list[str]] = {}
    for writer, writer_spec in spec.writers.items():
        for column in writer_spec.columns:
            owners.setdefault(column, []).append(writer)
    assert all(len(w) == 1 for w in owners.values()), "a column has two writers"
    order = {f"{w}_event_{part}" for w in spec.writers for part in ("time", "id")}
    assert set(columns) == set(spec.key_columns) | set(owners) | order
    assert set(ModelAbRow.model_fields) == set(columns)


def test_upsert_key_sql_touches_only_writer_columns() -> None:
    spec = load_live_spec()
    for writer in EnumAbWriter:
        sql = upsert_sql(writer)
        assert "ON CONFLICT (experiment_id, session_id, boundary_uuid)" in sql
        set_clause = sql.split("DO UPDATE SET ")[1].split(" WHERE ")[0]
        assigned = {part.split(" = ")[0] for part in set_clause.split(", ")}
        assert assigned == {
            *spec.writers[writer.value].columns,
            f"{writer.value}_event_time",
            f"{writer.value}_event_id",
        }
        assert f"{writer.value}_event_time" in sql.split(" WHERE ")[1]


def test_writer_cannot_set_foreign_column() -> None:
    with pytest.raises(ValidationError):
        _event(EnumAbWriter.SCORES, {"arm": "on"}, 1, "a")


def test_out_of_order_event_does_not_move_row_backwards() -> None:
    store = InMemoryAbStore()
    first = store.apply(_event(EnumAbWriter.SCORES, {"hl1_events": 5}, 10, "b"))
    assert first.applied
    late = store.apply(_event(EnumAbWriter.SCORES, {"hl1_events": 1}, 5, "a"))
    assert not late.applied
    assert store.rows()[("e1", "s1", "b1")].hl1_events == 5
    dup = store.apply(_event(EnumAbWriter.SCORES, {"hl1_events": 9}, 10, "b"))
    assert not dup.applied
    assert store.rows()[("e1", "s1", "b1")].hl1_events == 5
    tie_newer_id = store.apply(_event(EnumAbWriter.SCORES, {"hl1_events": 6}, 10, "c"))
    assert tie_newer_id.applied
    assert store.rows()[("e1", "s1", "b1")].hl1_events == 6


def test_out_of_order_is_per_writer() -> None:
    store = InMemoryAbStore()
    store.apply(_event(EnumAbWriter.SCORES, {"hl1_events": 5}, 10, "b"))
    older_other_writer = store.apply(
        _event(EnumAbWriter.ASSIGNMENT, {"arm": "on"}, 1, "a")
    )
    assert older_other_writer.applied
