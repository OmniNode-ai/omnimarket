"""OMN-19968 amendment 2: llm_call_metrics speaks the shared usage-source vocabulary.

omnibase_infra migration 077 (OMN-10382) moved ``usage_source_type`` to
``measured`` / ``estimated`` / ``unknown`` (EnumUsageSource). omnimarket's writer
still produced ``API`` / ``ESTIMATED`` / ``MISSING``, which a database migrated by
infra rejects (the ticket's lab step failed on exactly that, 2026-10-01 07:46Z).
"""

from __future__ import annotations

import logging
import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
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


# The adapter's own bookkeeping of one-time store steps. Named here as a literal,
# not imported, so these tests also run against code that predates the table.
STEPS_TABLE = "omnimarket_sqlite_store_steps"
VOCAB_STEP = "omn19968_usage_source_shared_vocabulary"


def _seed(path: Path, *labels: str, step_recorded: bool) -> None:
    """A store holding ``labels``; ``step_recorded=False`` is one written before
    the vocabulary step existed (by #3183's code, or by an older omnimarket)."""
    with closing(SqliteDatabaseAdapter(path)._connect()) as conn:
        for i, label in enumerate(labels):
            conn.execute(
                "INSERT INTO llm_call_metrics (model_id, input_hash, usage_source)"
                " VALUES ('m', ?, ?)",
                (f"h{i}", label),
            )
        if not step_recorded:
            conn.execute(f"DROP TABLE IF EXISTS {STEPS_TABLE}")
        conn.commit()


def _labels(path: Path) -> list[str]:
    """Read the file directly, past the adapter, so the read changes nothing."""
    with closing(sqlite3.connect(path)) as conn:
        return sorted(
            str(r[0]) for r in conn.execute("SELECT usage_source FROM llm_call_metrics")
        )


def _step_rows(path: Path) -> list[tuple[str, str]]:
    """The vocabulary step's record, the only step these tests are about; the
    store records its other one-time steps (OMN-20006's) in the same table."""
    with closing(sqlite3.connect(path)) as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (STEPS_TABLE,),
        ).fetchone()
        if exists is None:
            return []
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                f"SELECT step, applied_at FROM {STEPS_TABLE} WHERE step = ?",
                (VOCAB_STEP,),
            )
        ]


@contextmanager
def _read_only(path: Path) -> Iterator[None]:
    path.chmod(0o444)
    path.parent.chmod(0o555)
    try:
        if os.access(path, os.W_OK):
            pytest.fail(
                f"{path} is still writable with mode 0o444 (running as root?); "
                "this test cannot prove a read-only store is read"
            )
        yield
    finally:
        path.parent.chmod(0o755)
        path.chmod(0o644)


@contextmanager
def _held_by_another_writer(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Another connection holds a write transaction; the adapter does not wait.

    The default 5 s busy wait would only make a lock failure slow, so the
    adapter's connections get ``timeout=0`` and a lock shows as an error.
    """
    connect = sqlite3.connect

    def connect_without_waiting(
        database: Path, *args: Any, **kwargs: Any
    ) -> sqlite3.Connection:
        return connect(database, *args, **{**kwargs, "timeout": 0})

    writer = connect(path, isolation_level=None)
    writer.execute("BEGIN IMMEDIATE")
    monkeypatch.setattr(sqlite_database.sqlite3, "connect", connect_without_waiting)
    try:
        yield
    finally:
        monkeypatch.undo()
        writer.rollback()
        writer.close()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == sqlite_database.__name__ and record.levelno == logging.WARNING
    ]


def test_existing_sqlite_rows_move_onto_the_shared_vocabulary(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, "API", "ESTIMATED", "MISSING", "measured", step_recorded=False)

    SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert _labels(path) == ["estimated", "measured", "measured", "unknown"]


# --- the read lock (follow-up to #3183) -------------------------------------
# Failure modes, listed before the fix:
#  1. a read waits behind, or fails on, another connection's write transaction
#  2. a read-only store raises instead of being read
#  3. a store written before the step keeps a retired label
#  4. labels move but the step is not recorded (or the reverse) when a write fails
#  5. a read-only store holding retired labels raises, or is read silently
#  6. a busy writable store is passed off as read-only and read unrelabelled
#  7. a table without usage_source breaks the connect or records the step anyway
#  8. the step reads llm_call_metrics rows to decide (the OMN-17446 hook's shape;
#     checked by running the hook, not here)


def test_a_read_takes_no_write_lock_once_the_step_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, "measured", "unknown", step_recorded=True)

    with _held_by_another_writer(path, monkeypatch):
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert sorted(str(row["usage_source"]) for row in rows) == ["measured", "unknown"]


def test_a_read_only_store_with_the_step_recorded_is_read_without_a_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, "measured", step_recorded=True)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    with _read_only(path):
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert [row["usage_source"] for row in rows] == ["measured"]
    assert _warnings(caplog) == []


@pytest.mark.parametrize(
    ("retired", "shared"),
    [("API", "measured"), ("ESTIMATED", "estimated"), ("MISSING", "unknown")],
)
def test_a_store_without_the_step_has_each_retired_label_relabelled(
    tmp_path: Path, retired: str, shared: str
) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, retired, step_recorded=False)

    rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert [row["usage_source"] for row in rows] == [shared]


def test_the_step_is_recorded_with_the_relabel(tmp_path: Path) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, "API", step_recorded=False)

    SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert _labels(path) == ["measured"]
    steps = _step_rows(path)
    assert [step for step, _ in steps] == [VOCAB_STEP]
    assert steps[0][1]  # applied_at is stamped


def test_a_failed_step_record_leaves_the_labels_unchanged(tmp_path: Path) -> None:
    # The relabel and its record are one transaction: if the record cannot be
    # written, the labels must not have moved either, so the next connect
    # retries the whole step rather than trusting a half-done one.
    path = tmp_path / "evidence.sqlite"
    _seed(path, "API", step_recorded=False)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            f"CREATE TABLE {STEPS_TABLE} (step TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            f"CREATE TRIGGER refuse_step BEFORE INSERT ON {STEPS_TABLE} "
            "BEGIN SELECT RAISE(ABORT, 'step record refused'); END"
        )
        conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="step record refused"):
        SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert _labels(path) == ["API"]
    assert _step_rows(path) == []


def test_a_read_only_store_without_the_step_is_read_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, "API", "MISSING", step_recorded=False)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    with _read_only(path):
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert sorted(str(row["usage_source"]) for row in rows) == ["API", "MISSING"]
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert str(path) in warnings[0].getMessage()


def test_a_store_in_a_read_only_folder_without_the_step_is_read_as_it_is(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Only the folder is read-only, so the file stays writable but SQLite cannot
    # create its rollback journal: the extended code SQLITE_READONLY_DIRECTORY,
    # read-only only once masked to its primary code.
    path = tmp_path / "evidence.sqlite"
    _seed(path, "API", step_recorded=False)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    path.parent.chmod(0o555)
    try:
        if os.access(path.parent, os.W_OK):
            pytest.fail(
                f"{path.parent} is still writable with mode 0o555 (running as "
                "root?); this test cannot prove a store in a read-only folder is read"
            )
        rows = SqliteDatabaseAdapter(path).query("llm_call_metrics")
    finally:
        path.parent.chmod(0o755)

    assert [row["usage_source"] for row in rows] == ["API"]
    warnings = _warnings(caplog)
    assert len(warnings) == 1
    assert str(path) in warnings[0].getMessage()


def test_a_locked_store_without_the_step_raises_rather_than_reading_unrelabelled(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "evidence.sqlite"
    _seed(path, "API", step_recorded=False)
    caplog.set_level(logging.WARNING, logger=sqlite_database.__name__)

    with (
        _held_by_another_writer(path, monkeypatch),
        pytest.raises(sqlite3.OperationalError, match="locked"),
    ):
        SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert _warnings(caplog) == []
    assert _labels(path) == ["API"]


def test_a_table_without_usage_source_connects_and_records_no_step(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.sqlite"
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE llm_call_metrics (id INTEGER PRIMARY KEY, "
            "correlation_id TEXT, model_id TEXT NOT NULL, input_hash TEXT)"
        )
        conn.commit()

    SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert _step_rows(path) == []


def test_a_retired_label_written_after_the_step_stays_as_written(
    tmp_path: Path,
) -> None:
    # Stated limit, asserted so that widening it is deliberate: the step runs
    # once per store, like Postgres migration 0003. A retired label an older
    # omnimarket writes after that is not relabelled here. (On Postgres the
    # renamed enum refuses it instead.)
    path = tmp_path / "evidence.sqlite"
    _seed(path, step_recorded=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "INSERT INTO llm_call_metrics (model_id, input_hash, usage_source) "
            "VALUES ('m', 'late', 'API')"
        )
        conn.commit()

    SqliteDatabaseAdapter(path).query("llm_call_metrics")

    assert _labels(path) == ["API"]


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
