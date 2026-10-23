# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read local delegation records for the metering summary (OMN-18697).

This is the I/O edge of the local metering path. It opens the same SQLite file
:mod:`omnimarket.projection.sqlite_database` writes, turns each row into a
typed :class:`ModelMeteringRecord`, and hands the records to the pure summary
node. Nothing here decides anything about cost; it only reports what the record
says, including when the record says nothing.

TWO STORAGE FACTS DECIDE THE SHAPE OF THIS MODULE.

**``created_at`` is mixed-type, and the obvious window query is silently
wrong.** The DDL declares ``created_at REAL NOT NULL DEFAULT (strftime('%s',
'now'))``, but SQLite is dynamically typed and the projection handler writes an
ISO-8601 string. On the operator's Mac on 2026-09-18 the column held 23,290
TEXT rows and 7 REAL rows in the same table. SQLite orders storage classes
before values -- INTEGER and REAL sort below TEXT -- so
``WHERE created_at >= strftime('%s','now') - 7*86400`` matches **every** TEXT
row regardless of date. A hand query in that shape reported 23,285 runs and
$631.78 "over seven days"; the true seven-day figures were 346 runs and
$123.62. The filter therefore happens **in Python, after normalisation**, and
never in SQL. Correcting the SQL to compare against an ISO string would fix
the text rows and silently drop the real ones.

**Zero is the table's default, not a measurement.** ``tokens_input``,
``tokens_output`` and ``cost_savings_usd`` are all ``NOT NULL DEFAULT 0``, so a
run that recorded nothing is byte-identical to a run that genuinely used
nothing. A row whose token counts are BOTH zero is reported as ``None`` --
unrecorded -- because a completed delegation that consumed no tokens in either
direction is not a thing that happens, whereas a writer that never populated
the columns is the overwhelmingly common case here. ``cost_usd`` is nullable
and is passed through honestly: null in, ``None`` out.

The connection is read-only (``mode=ro``) and no statement in this module
writes. A metering readout must never be able to damage the evidence it reads.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_record_row import (
    METERING_CAUSE_COLUMN,
    METERING_COLUMNS,
    metering_record_from_row,
    normalise_occurred_at,
)
from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelMeteringRecord,
)
from omnimarket.projection.sqlite_database import default_evidence_db_path

__all__ = [
    "METERING_COLUMNS",
    "MeteringRecordsUnavailableError",
    "default_metering_db_path",
    "metering_record_from_row",
    "normalise_occurred_at",
    "read_metering_records",
]

_SELECT = f"SELECT {', '.join(METERING_COLUMNS)} FROM delegation_events"
# OMN-19970: provenance column, present on stores written since migration 0049's
# local twin. Read only when the store has it; a store written before the label
# existed holds real rows only.
_DATA_SOURCE_COLUMN = "data_source"
_FIXTURE = "fixture"


class MeteringRecordsUnavailableError(RuntimeError):
    """The local evidence database could not be read.

    Raised rather than returning an empty list, because "no delegation has ever
    run here" and "the evidence store is unreadable" are different answers and
    a metering readout that conflates them reports a confident zero for a
    broken install.
    """


def default_metering_db_path() -> Path:
    """The canonical local delegation evidence database.

    Delegated to :mod:`omnimarket.projection.sqlite_database` so the reader and
    the writer cannot disagree about which file is the evidence store.
    """
    return default_evidence_db_path()


def _to_record(row: sqlite3.Row) -> ModelMeteringRecord | None:
    # sqlite3.Row iterates values; keys() names the columns.
    return metering_record_from_row(dict(zip(row.keys(), row, strict=True)))


def _iter_rows(db_path: Path) -> Iterator[sqlite3.Row]:
    if not db_path.exists():
        raise MeteringRecordsUnavailableError(
            f"no local delegation evidence database at {db_path}"
        )
    # Read-only URI: a readout must not be able to damage the evidence.
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)  # no-contract-check: projection boundary
    except sqlite3.Error as exc:
        raise MeteringRecordsUnavailableError(
            f"cannot open the local delegation evidence database at {db_path}: {exc}"
        ) from exc
    try:
        conn.row_factory = sqlite3.Row
        try:
            columns = {
                str(info[1])
                for info in conn.execute("PRAGMA table_info(delegation_events)")
            }
            optional = tuple(
                column
                for column in (_DATA_SOURCE_COLUMN, METERING_CAUSE_COLUMN)
                if column in columns
            )
            select = (
                f"SELECT {', '.join((*METERING_COLUMNS, *optional))} "
                "FROM delegation_events"
                if optional
                else _SELECT
            )
            cursor = conn.execute(select)
        except sqlite3.Error as exc:
            raise MeteringRecordsUnavailableError(
                f"cannot read delegation_events from {db_path}: {exc}"
            ) from exc
        yield from cursor
    finally:
        conn.close()


def read_metering_records(
    *,
    db_path: Path | None = None,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
    include_fixtures: bool = False,
) -> tuple[ModelMeteringRecord, ...]:
    """Read delegation records in ``[window_start, window_end)``.

    OMN-19970: rows the dev and demo seed wrote (``data_source = 'fixture'``)
    are measured nothing and are skipped unless ``include_fixtures`` is set, so
    a seeded store never inflates a measured spend or savings figure.

    Both bounds are optional; omitting ``window_start`` reads all time. The
    window is applied in Python against normalised instants, never in SQL --
    the module docstring records the seven-day figure a SQL window produced on
    this column and why it was wrong.

    Rows whose ``created_at`` cannot be parsed are omitted rather than dated to
    now, so a corrupt timestamp shrinks the window's population instead of
    inventing a run inside it.
    """
    resolved = db_path if db_path is not None else default_metering_db_path()
    records: list[ModelMeteringRecord] = []
    for row in _iter_rows(resolved):
        if (
            not include_fixtures
            # sqlite3.Row: `in row` tests values, so the column names are keys().
            and _DATA_SOURCE_COLUMN in row.keys()  # noqa: SIM118
            and row[_DATA_SOURCE_COLUMN] == _FIXTURE
        ):
            continue
        record = _to_record(row)
        if record is None:
            continue
        if window_start is not None and record.occurred_at < window_start:
            continue
        if window_end is not None and record.occurred_at >= window_end:
            continue
        records.append(record)
    records.sort(key=lambda r: (r.occurred_at, r.correlation_id))
    return tuple(records)
