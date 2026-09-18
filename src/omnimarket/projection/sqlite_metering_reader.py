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
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from omnimarket.nodes.node_metering_summary_compute.models.model_metering_summary import (
    ModelMeteringRecord,
)
from omnimarket.projection.sqlite_database import default_evidence_db_path

__all__ = [
    "METERING_COLUMNS",
    "MeteringRecordsUnavailableError",
    "default_metering_db_path",
    "normalise_occurred_at",
    "read_metering_records",
]

# Only the columns the summary needs. Naming them keeps `prompt_text` and
# `response_text` -- which dominate the file's 120 MB -- out of the read.
METERING_COLUMNS = (
    "correlation_id",
    "created_at",
    "delegated_to",
    "model_name",
    "task_type",
    "tokens_input",
    "tokens_output",
    "cost_usd",
    "cost_savings_usd",
)

_SELECT = f"SELECT {', '.join(METERING_COLUMNS)} FROM delegation_events"


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


def normalise_occurred_at(raw: object) -> datetime | None:
    """Turn a ``created_at`` cell into an aware UTC instant, or ``None``.

    Accepts both storage classes the column actually holds: an ISO-8601 string
    written by the projection handler, and a unix epoch written by the DDL
    default. A naive string is read as UTC, which is what the writer emits.
    Anything unparseable returns ``None`` so the row is dropped from the window
    rather than silently dated to now.
    """
    if isinstance(raw, int | float):
        return datetime.fromtimestamp(float(raw), tz=UTC)
    if isinstance(raw, str) and raw:
        text = raw.strip()
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            # A numeric epoch stored with TEXT affinity, e.g. '1781552380.36'.
            try:
                return datetime.fromtimestamp(float(text), tz=UTC)
            except ValueError:
                return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _to_decimal(raw: object) -> Decimal | None:
    """A nullable money cell as an exact Decimal, or ``None`` when unrecorded."""
    if raw is None:
        return None
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return None


def _to_record(row: sqlite3.Row) -> ModelMeteringRecord | None:
    occurred_at = normalise_occurred_at(row["created_at"])
    if occurred_at is None:
        return None

    tokens_in = int(row["tokens_input"] or 0)
    tokens_out = int(row["tokens_output"] or 0)
    # Both zero is the table default, not a measurement. See the module
    # docstring: a completed delegation consuming no tokens in either
    # direction does not happen; an unpopulated pair of columns does.
    unrecorded_tokens = tokens_in == 0 and tokens_out == 0

    return ModelMeteringRecord(
        correlation_id=str(row["correlation_id"]),
        occurred_at=occurred_at,
        model=str(row["delegated_to"] or row["model_name"] or ""),
        task_type=str(row["task_type"] or ""),
        tokens_in=None if unrecorded_tokens else tokens_in,
        tokens_out=None if unrecorded_tokens else tokens_out,
        spend_usd=_to_decimal(row["cost_usd"]),
        recorded_savings_usd=_to_decimal(row["cost_savings_usd"]),
    )


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
            cursor = conn.execute(_SELECT)
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
) -> tuple[ModelMeteringRecord, ...]:
    """Read delegation records in ``[window_start, window_end)``.

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
