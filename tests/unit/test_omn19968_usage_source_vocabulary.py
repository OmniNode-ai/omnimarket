"""OMN-19968 amendment 2: llm_call_metrics speaks the shared usage-source vocabulary.

omnibase_infra migration 077 (OMN-10382) moved ``usage_source_type`` to
``measured`` / ``estimated`` / ``unknown`` (EnumUsageSource). omnimarket's writer
still produced ``API`` / ``ESTIMATED`` / ``MISSING``, which a database migrated by
infra rejects (the ticket's lab step failed on exactly that, 2026-10-01 07:46Z).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.nodes.node_projection_llm_cost.handlers.row_llm_call_metrics import (
    build_llm_call_metrics_row,
)
from omnimarket.projection import sqlite_database
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

SHARED = {member.value for member in EnumUsageSource}
RETIRED = {"API", "ESTIMATED", "MISSING"}
MIGRATIONS = (
    Path(__file__).parents[2]
    / "src/omnimarket/nodes/node_projection_llm_cost/migrations"
)
VOCAB_MIGRATION = MIGRATIONS / "0003_usage_source_shared_vocabulary.sql"


def _event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "correlation_id": "19968000-0000-4000-8000-0000000000aa",
        "model_id": "claude-sonnet-5-5",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "timestamp": "2026-10-01T08:00:00+00:00",
    }
    event.update(overrides)
    return event


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("MEASURED", "measured"),
        ("measured", "measured"),
        ("API", "measured"),
        ("api", "measured"),
        ("ESTIMATED", "estimated"),
        ("estimated", "estimated"),
        ("MISSING", "unknown"),
        ("UNKNOWN", "unknown"),
        ("unknown", "unknown"),
        ("something-else", "unknown"),
    ],
)
def test_row_writes_the_shared_vocabulary(raw: str, expected: str) -> None:
    row = build_llm_call_metrics_row(_event(usage_source=raw))

    assert row["usage_source"] == expected
    assert row["usage_is_estimated"] is (expected != "measured")


def test_absent_usage_source_is_unknown_and_estimated() -> None:
    row = build_llm_call_metrics_row(_event())

    assert row["usage_source"] == "unknown"
    assert row["usage_is_estimated"] is True


def test_nested_usage_normalized_source_maps_too() -> None:
    row = build_llm_call_metrics_row(_event(usage_normalized={"source": "api"}))

    assert row["usage_source"] == "measured"
    assert row["usage_is_estimated"] is False


def test_sqlite_schema_defaults_to_the_shared_vocabulary(tmp_path: Path) -> None:
    adapter = SqliteDatabaseAdapter(tmp_path / "evidence.sqlite")
    with adapter._connect() as conn:  # the adapter's own schema bootstrap
        conn.execute(
            "INSERT INTO llm_call_metrics (model_id, input_hash) VALUES ('m', 'h1')"
        )
        value = conn.execute("SELECT usage_source FROM llm_call_metrics").fetchone()[0]

    assert value == "unknown"


def test_existing_sqlite_rows_move_onto_the_shared_vocabulary(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    with adapter._connect() as conn:
        for i, old in enumerate(("API", "ESTIMATED", "MISSING", "measured")):
            conn.execute(
                "INSERT INTO llm_call_metrics (model_id, input_hash, usage_source)"
                " VALUES ('m', ?, ?)",
                (f"h{i}", old),
            )
        conn.commit()

    with adapter._connect() as conn:  # reconnect: the reconcile step runs again
        values = sorted(
            r[0] for r in conn.execute("SELECT usage_source FROM llm_call_metrics")
        )

    assert values == ["estimated", "measured", "measured", "unknown"]


def test_no_retired_value_remains_in_the_writer_or_sqlite_schema() -> None:
    row_module = Path(build_llm_call_metrics_row.__code__.co_filename).read_text(
        encoding="utf-8"
    )
    sqlite_module = Path(sqlite_database.__file__).read_text(encoding="utf-8")

    for retired in RETIRED:
        assert f'"{retired}"' not in row_module, retired
    assert "DEFAULT 'MISSING'" not in sqlite_module


def test_forward_migration_renames_the_retired_labels_in_place() -> None:
    assert VOCAB_MIGRATION.is_file(), "a forward migration must move existing databases"
    sql = VOCAB_MIGRATION.read_text(encoding="utf-8")

    for old, new in (
        ("API", "measured"),
        ("ESTIMATED", "estimated"),
        ("MISSING", "unknown"),
    ):
        assert f"RENAME VALUE '{old}' TO '{new}'" in sql
    # resolve the type through search_path, never by name across every schema
    assert "to_regtype('usage_source_type')" in sql
    assert "SET DEFAULT 'unknown'" in sql


def test_migration_number_is_unique_in_the_node() -> None:
    numbers = [p.name.split("_", 1)[0] for p in MIGRATIONS.glob("*.sql")]

    assert numbers.count("0003") == 1
