# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local metering reader: mixed-type timestamps and honest unknowns (OMN-18697).

The fixture below reproduces the exact storage shape found on the operator's
Mac on 2026-09-18 -- an ISO-8601 TEXT ``created_at`` on almost every row and a
REAL unix epoch on a handful -- because that mix is what made the obvious SQL
window query silently return the all-time population.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from omnimarket.nodes.node_metering_summary_compute import EnumMeteringMeasurement
from omnimarket.projection.sqlite_metering_reader import (
    MeteringRecordsUnavailableError,
    default_metering_db_path,
    normalise_occurred_at,
    read_metering_records,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)

_DDL = """
CREATE TABLE delegation_events (
    correlation_id   TEXT NOT NULL UNIQUE,
    created_at       REAL NOT NULL DEFAULT (strftime('%s','now')),
    delegated_to     TEXT NOT NULL DEFAULT '',
    model_name       TEXT NOT NULL DEFAULT '',
    task_type        TEXT NOT NULL DEFAULT '',
    tokens_input     INTEGER NOT NULL DEFAULT 0,
    tokens_output    INTEGER NOT NULL DEFAULT 0,
    cost_usd         REAL,
    cost_savings_usd REAL NOT NULL DEFAULT 0.0
)
"""


@pytest.fixture
def db(tmp_path: Path) -> Path:
    """A database carrying both storage classes in ``created_at``."""
    path = tmp_path / "delegation.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(_DDL)
    rows = [
        # Recent, ISO TEXT, fully measured.
        (
            "fresh-text",
            (_NOW - timedelta(hours=1)).isoformat(),
            "Qwen3.8-27B",
            "Qwen3.8-27B",
            "classify",
            1000,
            1000,
            0.0,
            0.09,
        ),
        # Recent, REAL epoch — the DDL default's storage class.
        (
            "fresh-real",
            (_NOW - timedelta(hours=2)).timestamp(),
            "Qwen3.8-27B",
            "Qwen3.8-27B",
            "classify",
            200,
            100,
            0.001,
            0.01,
        ),
        # Old, ISO TEXT — a SQL numeric window would wrongly include this.
        (
            "stale-text",
            (_NOW - timedelta(days=90)).isoformat(),
            "Qwen3.8-27B",
            "Qwen3.8-27B",
            "classify",
            500,
            500,
            0.0,
            0.045,
        ),
        # Recent, no token counts and no cost: the unmeasured row.
        (
            "unmeasured",
            (_NOW - timedelta(hours=3)).isoformat(),
            "Qwen3.8-27B",
            "Qwen3.8-27B",
            "classify",
            0,
            0,
            None,
            0.0,
        ),
    ]
    conn.executemany(
        "INSERT INTO delegation_events (correlation_id, created_at, delegated_to, "
        "model_name, task_type, tokens_input, tokens_output, cost_usd, "
        "cost_savings_usd) VALUES (?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return path


class TestMixedTypeTimestamps:
    """The defect that made an all-time figure read as a seven-day one."""

    def test_both_storage_classes_are_read(self) -> None:
        text = normalise_occurred_at("2026-09-18T12:00:00+00:00")
        real = normalise_occurred_at(_NOW.timestamp())
        assert text == _NOW
        assert real == _NOW

    def test_a_naive_string_is_read_as_utc(self) -> None:
        assert normalise_occurred_at("2026-09-18T12:00:00") == _NOW

    def test_an_epoch_stored_as_text_is_still_read(self) -> None:
        assert normalise_occurred_at(str(_NOW.timestamp())) == _NOW

    def test_an_unparseable_cell_is_none_not_now(self) -> None:
        assert normalise_occurred_at("not a date") is None
        assert normalise_occurred_at(None) is None
        assert normalise_occurred_at("") is None

    def test_the_window_excludes_an_old_text_row(self, db: Path) -> None:
        """A numeric SQL window matches every TEXT row; this one must not."""
        records = read_metering_records(
            db_path=db, window_start=_NOW - timedelta(days=7), window_end=_NOW
        )
        ids = {r.correlation_id for r in records}
        assert "stale-text" not in ids
        assert ids == {"fresh-text", "fresh-real", "unmeasured"}

    def test_the_window_includes_a_recent_real_row(self, db: Path) -> None:
        """Repairing the SQL to compare ISO strings would drop this one."""
        records = read_metering_records(
            db_path=db, window_start=_NOW - timedelta(days=7), window_end=_NOW
        )
        assert "fresh-real" in {r.correlation_id for r in records}

    def test_an_unbounded_read_returns_every_row(self, db: Path) -> None:
        assert len(read_metering_records(db_path=db)) == 4

    def test_records_come_back_in_time_order(self, db: Path) -> None:
        records = read_metering_records(db_path=db)
        assert [r.correlation_id for r in records] == [
            "stale-text",
            "unmeasured",
            "fresh-real",
            "fresh-text",
        ]


class TestUnknownsSurviveTheRead:
    """AC3 at the reader: the table's defaults are not measurements."""

    def test_both_token_counts_zero_reads_as_unrecorded(self, db: Path) -> None:
        record = next(
            r
            for r in read_metering_records(db_path=db)
            if r.correlation_id == "unmeasured"
        )
        assert record.tokens_in is None
        assert record.tokens_out is None
        assert record.spend_usd is None
        assert record.measurement is EnumMeteringMeasurement.UNKNOWN_TOKENS

    def test_a_null_cost_reads_as_none_not_zero(self, db: Path) -> None:
        record = next(
            r
            for r in read_metering_records(db_path=db)
            if r.correlation_id == "unmeasured"
        )
        assert record.spend_usd is None

    def test_a_recorded_zero_cost_reads_as_zero(self, db: Path) -> None:
        record = next(
            r
            for r in read_metering_records(db_path=db)
            if r.correlation_id == "fresh-text"
        )
        assert record.spend_usd == Decimal("0")
        assert record.measurement is EnumMeteringMeasurement.MEASURED

    def test_money_is_exact_decimal_not_float(self, db: Path) -> None:
        record = next(
            r
            for r in read_metering_records(db_path=db)
            if r.correlation_id == "fresh-real"
        )
        assert record.spend_usd == Decimal("0.001")
        assert isinstance(record.spend_usd, Decimal)


class TestUnavailableStoreIsNotAnEmptyOne:
    """ "Nothing ran here" and "the store is broken" are different answers."""

    def test_a_missing_database_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(MeteringRecordsUnavailableError, match="no local"):
            read_metering_records(db_path=tmp_path / "absent.sqlite")

    def test_a_database_without_the_table_refuses(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.sqlite"
        sqlite3.connect(path).close()
        with pytest.raises(MeteringRecordsUnavailableError, match="delegation_events"):
            read_metering_records(db_path=path)

    def test_an_empty_table_is_an_empty_tuple(self, tmp_path: Path) -> None:
        path = tmp_path / "fresh.sqlite"
        conn = sqlite3.connect(path)
        conn.execute(_DDL)
        conn.commit()
        conn.close()
        assert read_metering_records(db_path=path) == ()


class TestReadIsReadOnly:
    def test_the_reader_cannot_write_to_the_evidence_store(self, db: Path) -> None:
        before = db.read_bytes()
        read_metering_records(db_path=db)
        assert db.read_bytes() == before

    def test_the_default_path_matches_the_writer(self) -> None:
        """Reader and writer must not disagree about which file is evidence."""
        from omnimarket.projection.sqlite_database import default_evidence_db_path

        assert default_metering_db_path() == default_evidence_db_path()
