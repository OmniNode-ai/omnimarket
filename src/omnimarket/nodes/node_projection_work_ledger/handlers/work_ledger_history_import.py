# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Import the ledger's archive splits and live file once, with a legacy_ordinal (OMN-21947, L3).

``plan_history_import`` is pure: it reads the files in file order (archive splits by date, then
the live file), gives each row a ``legacy_ordinal`` (its 1-based place among all row lines) and
builds the ``ledger_line_citation`` entries. ``apply_history_import`` writes the plan to a
database inside one transaction and is idempotent:

* a row not yet in ``work_ledger_rows`` is inserted with ``source='archive-import'``, a null
  ``ledger_seq`` and its ``legacy_ordinal``, and its state ops are applied with the
  projection's own guarded SQL;
* a row already there with no ``ledger_seq`` gains its ``legacy_ordinal`` only if it has none;
* a row that carries a ``ledger_seq`` is never written: the assigned sequence is not read for
  change, renumbered or touched. The transaction compares a hash of the assigned column before
  and after and rolls back when it moved.

One ordering contract for every reader and for the L12 bootstrap (``ordering_key``): rows
without a ``ledger_seq`` first, by ``legacy_ordinal``; then rows with one, by ``ledger_seq``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    fold_row,
    parse_stamp,
)
from omnimarket.handlers.work_ledger_text import ledger_order_key
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_projection import (
    _op_args,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.handler_work_ledger_write_guard import (
    HandlerWorkLedgerWriteGuard,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_citation import (
    build_citations,
    infer_header_shifts,
)
from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_history_files import (
    LedgerHistory,
    read_ledger_history,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_history import (
    IMPORT_SOURCE,
    ModelHistoryImportPlan,
    ModelHistoryImportReport,
    ModelHistoryRow,
    ModelLedgerSeqFingerprint,
)

ROWS_TABLE = "omninode_internal.work_ledger_rows"
CITATION_TABLE = "omninode_internal.ledger_line_citation"
DEFAULT_LEDGER_ID = "rolling-work-ledger"
DEFAULT_SCHEMA = "omninode_internal"
_ADVISORY_LOCK = "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))"

_FINGERPRINT = f"""
    SELECT count(*) FILTER (WHERE ledger_seq IS NOT NULL) AS assigned,
           count(DISTINCT ledger_seq) AS distinct_seq,
           min(ledger_seq) AS min_seq,
           max(ledger_seq) AS max_seq,
           encode(sha256(convert_to(coalesce(
               string_agg(row_id || ':' || ledger_seq::text, ',' ORDER BY row_id)
                   FILTER (WHERE ledger_seq IS NOT NULL), ''), 'UTF8')), 'hex') AS digest,
           min(row_ts) AS min_row_ts,
           count(*) AS total
    FROM {ROWS_TABLE} WHERE ledger_id = $1
"""
_EXISTING_IDS = f"SELECT row_id FROM {ROWS_TABLE} WHERE ledger_id = $1"
_INSERT_ROW = f"""
    INSERT INTO {ROWS_TABLE}
        (row_id, ledger_id, row_ts, row_type, row_lane, tickets, raw_row, source,
         projected_at, legacy_ordinal)
    VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10)
    ON CONFLICT (row_id) DO NOTHING
"""
_SET_ORDINALS = f"""
    UPDATE {ROWS_TABLE} SET legacy_ordinal = v.ordinal
    FROM unnest($1::text[], $2::bigint[]) AS v(row_id, ordinal)
    WHERE {ROWS_TABLE}.row_id = v.row_id
      AND {ROWS_TABLE}.ledger_seq IS NULL
      AND {ROWS_TABLE}.legacy_ordinal IS NULL
"""
_EXISTING_CITATIONS = f"""
    SELECT file_key, generation, line_no, row_id FROM {CITATION_TABLE} WHERE ledger_id = $1
"""
_INSERT_CITATION = f"""
    INSERT INTO {CITATION_TABLE} (ledger_id, file_key, generation, line_no, row_id)
    VALUES ($1, $2, $3, $4, $5)
    ON CONFLICT (ledger_id, file_key, generation, line_no) DO NOTHING
"""


class LedgerSeqChangedError(RuntimeError):
    """The assigned ledger_seq column moved during the import; the transaction is rolled back."""


class CitationConflictError(RuntimeError):
    """A citation key already holds a different row_id; the table is immutable, so stop."""


def ordering_key(
    ledger_seq: int | None, legacy_ordinal: int | None, row_id: str
) -> tuple[int, int, str]:
    """The one ordering contract (``ledger_order_key``) with the row id as the final tiebreak."""
    bucket, position = ledger_order_key(ledger_seq, legacy_ordinal)
    return (bucket, position, row_id)


def plan_history_import(
    history: LedgerHistory,
    ledger_id: str = DEFAULT_LEDGER_ID,
    *,
    infer_shifts: bool = False,
) -> ModelHistoryImportPlan:
    """Rows in file order with their ordinals, and the citation table. Pure; reads nothing."""
    rows: list[ModelHistoryRow] = []
    seen: set[str] = set()
    duplicates = 0
    unprojectable: list[str] = []
    for ordinal, file_row in enumerate(history.rows, start=1):
        log_only = False
        record: ModelWorkLedgerRowRecord | _LogOnlyRecord
        try:
            record = fold_row(
                ModelWorkLedgerFoldRequest(
                    raw_row=file_row.raw_row, ledger_id=ledger_id, source=IMPORT_SOURCE
                )
            ).row
        except WorkLedgerFoldError:
            record = _log_only_record(file_row.raw_row, ledger_id)
            unprojectable.append(record.row_id)
            log_only = True
        if record.row_id in seen:
            duplicates += 1
            continue
        seen.add(record.row_id)
        rows.append(
            ModelHistoryRow(
                row_id=record.row_id,
                ledger_id=ledger_id,
                row_ts=record.row_ts,
                row_type=record.row_type,
                row_lane=record.row_lane,
                tickets=tuple(record.tickets),
                raw_row=record.raw_row,
                legacy_ordinal=ordinal,
                file_key=file_row.file_key,
                first_line=file_row.first_line,
                log_only=log_only,
            )
        )
    shifts = infer_header_shifts(history) if infer_shifts else {}
    citations, resolutions = build_citations(history, ledger_id, shifts)
    first = rows[0] if rows else None
    return ModelHistoryImportPlan(
        ledger_id=ledger_id,
        rows=tuple(rows),
        citations=citations,
        resolutions=resolutions,
        duplicate_rows=duplicates,
        unprojectable_rows=tuple(unprojectable),
        header_shifts=shifts,
        oldest_row_ts=first.row_ts if first else None,
        oldest_file_key=first.file_key if first else None,
    )


_LANE_CELL = re.compile(r"(?:^|\| )lane=([^ |]+)")
_TICKET_CELL = re.compile(r"(?:^|\| )tickets?=([^|]*)")


class _LogOnlyRecord:
    """The log fields of a row whose type the projection's parser refuses (a legacy type)."""

    __slots__ = ("raw_row", "row_id", "row_lane", "row_ts", "row_type", "tickets")

    def __init__(self, raw_row: str, ledger_id: str) -> None:
        self.raw_row = raw_row.strip()
        self.row_id = work_ledger_row_id(raw_row)
        cells = [cell.strip() for cell in self.raw_row.split("|")]
        self.row_ts = parse_stamp(cells[0])
        self.row_type = cells[1] if len(cells) > 1 else "unknown"
        lane = _LANE_CELL.search(self.raw_row)
        self.row_lane = lane.group(1) if lane else None
        tickets = _TICKET_CELL.search(self.raw_row)
        self.tickets = (
            tuple(dict.fromkeys(_TICKET_ID.findall(tickets.group(1))))
            if tickets
            else ()
        )


_TICKET_ID = re.compile(r"OMN-\d+")


def _log_only_record(raw_row: str, ledger_id: str) -> _LogOnlyRecord:
    return _LogOnlyRecord(raw_row, ledger_id)


def _in(sql: str, schema: str) -> str:
    """The statement against ``schema`` (a disposable schema in a test, else the real one)."""
    return (
        sql
        if schema == DEFAULT_SCHEMA
        else sql.replace(f"{DEFAULT_SCHEMA}.", f"{schema}.")
    )


async def ledger_seq_fingerprint(
    conn: Any, ledger_id: str, schema: str = DEFAULT_SCHEMA
) -> ModelLedgerSeqFingerprint:
    """The assigned ledger_seq column as count, distinct count, range and one sha256."""
    record = await conn.fetchrow(_in(_FINGERPRINT, schema), ledger_id)
    return ModelLedgerSeqFingerprint(
        assigned_rows=int(record["assigned"]),
        distinct_seq=int(record["distinct_seq"]),
        min_seq=record["min_seq"],
        max_seq=record["max_seq"],
        sha256=str(record["digest"]),
        min_row_ts=record["min_row_ts"],
        total_rows=int(record["total"]),
    )


async def apply_history_import(
    conn: Any,
    plan: ModelHistoryImportPlan,
    *,
    now: datetime | None = None,
    chunk: int = 5000,
    schema: str = DEFAULT_SCHEMA,
) -> ModelHistoryImportReport:
    """Write the plan in one transaction. Idempotent; rolls back if ledger_seq moved."""
    stamp = now or datetime.now(UTC)
    async with conn.transaction():
        await conn.execute(_ADVISORY_LOCK, f"work-ledger:{plan.ledger_id}")
        before = await ledger_seq_fingerprint(conn, plan.ledger_id, schema)
        existing = {
            r["row_id"]
            for r in await conn.fetch(_in(_EXISTING_IDS, schema), plan.ledger_id)
        }
        new_rows = [row for row in plan.rows if row.row_id not in existing]
        old_rows = [row for row in plan.rows if row.row_id in existing]

        for start in range(0, len(new_rows), chunk):
            await conn.executemany(
                _in(_INSERT_ROW, schema),
                [
                    (
                        row.row_id,
                        row.ledger_id,
                        row.row_ts,
                        row.row_type,
                        row.row_lane,
                        json.dumps(list(row.tickets)),
                        row.raw_row,
                        IMPORT_SOURCE,
                        stamp,
                        row.legacy_ordinal,
                    )
                    for row in new_rows[start : start + chunk]
                ],
            )

        ordinals_set = 0
        for start in range(0, len(old_rows), chunk):
            part = old_rows[start : start + chunk]
            status = await conn.execute(
                _in(_SET_ORDINALS, schema),
                [row.row_id for row in part],
                [row.legacy_ordinal for row in part],
            )
            ordinals_set += int(status.rsplit(" ", 1)[-1])

        ops_applied = 0
        by_statement: dict[str, list[tuple[Any, ...]]] = {}
        for row in new_rows:
            if row.log_only:
                continue
            result = fold_row(
                ModelWorkLedgerFoldRequest(
                    raw_row=row.raw_row, ledger_id=row.ledger_id, source=IMPORT_SOURCE
                )
            )
            for op in result.ops:
                sql, args = _op_args(op, stamp)
                sql = _in(sql, schema)
                by_statement.setdefault(sql, []).append(args)
                ops_applied += 1
        for sql, arg_sets in by_statement.items():
            for start in range(0, len(arg_sets), chunk):
                await conn.executemany(sql, arg_sets[start : start + chunk])

        held = {
            (r["file_key"], r["generation"], r["line_no"]): r["row_id"]
            for r in await conn.fetch(_in(_EXISTING_CITATIONS, schema), plan.ledger_id)
        }
        fresh = []
        for entry in plan.citations:
            key = (entry.file_key, entry.generation, entry.line_no)
            if key in held:
                if held[key] != entry.row_id:
                    raise CitationConflictError(
                        f"citation {key} already names {held[key]}"
                    )
                continue
            fresh.append(
                (
                    entry.ledger_id,
                    entry.file_key,
                    entry.generation,
                    entry.line_no,
                    entry.row_id,
                )
            )
        for start in range(0, len(fresh), chunk):
            await conn.executemany(
                _in(_INSERT_CITATION, schema), fresh[start : start + chunk]
            )

        after = await ledger_seq_fingerprint(conn, plan.ledger_id, schema)
        if (
            after.sha256 != before.sha256
            or after.assigned_rows != before.assigned_rows
            or after.distinct_seq != before.distinct_seq
        ):
            raise LedgerSeqChangedError(
                f"ledger_seq moved: before {before.sha256} ({before.assigned_rows} rows), "
                f"after {after.sha256} ({after.assigned_rows} rows)"
            )
    return ModelHistoryImportReport(
        planned_rows=len(plan.rows),
        inserted_rows=len(new_rows),
        ordinals_set_on_existing=ordinals_set,
        state_ops_applied=ops_applied,
        citations_inserted=len(fresh),
        before=before,
        after=after,
    )


def _plan_summary(plan: ModelHistoryImportPlan) -> dict[str, Any]:
    unresolved = sum(1 for r in plan.resolutions if r.row_id is None)
    return {
        "ledger_id": plan.ledger_id,
        "rows": len(plan.rows),
        "oldest_row_ts": plan.oldest_row_ts.isoformat() if plan.oldest_row_ts else None,
        "oldest_file": plan.oldest_file_key,
        "duplicate_rows": plan.duplicate_rows,
        "log_only_rows": len(plan.unprojectable_rows),
        "header_shifts": {str(k): v for k, v in sorted(plan.header_shifts.items())},
        "citation_entries": len(plan.citations),
        "citations": len(plan.resolutions),
        "citations_unresolved": unresolved,
    }


def _loopback(dsn: str) -> bool:
    return not HandlerWorkLedgerWriteGuard.dsn_is_real(dsn)


async def _run(dsn: str, plan: ModelHistoryImportPlan) -> ModelHistoryImportReport:
    import asyncpg  # lazy: only an --apply run needs the driver

    conn = await asyncpg.connect(dsn)
    try:
        return await apply_history_import(conn, plan)
    finally:
        await conn.close()


def backfill_main(argv: list[str] | None = None) -> int:
    """The ``backfill`` command: plan the history import, or ``--apply`` it to an isolated database.

    Exit 0 is a plan or an applied import whose ledger_seq hash held; 2 is a refusal or error.
    """
    parser = argparse.ArgumentParser(
        prog="work_ledger_history_import", description=__doc__
    )
    parser.add_argument(
        "--ledger", type=Path, help="live markdown ledger (default $ONEX_LEDGER_PATH)"
    )
    parser.add_argument(
        "--archive-dir", type=Path, help="archive splits (default <ledger dir>/archive)"
    )
    parser.add_argument("--ledger-id", default=DEFAULT_LEDGER_ID)
    parser.add_argument(
        "--apply", action="store_true", help="write the plan (default: print it)"
    )
    parser.add_argument(
        "--dsn-env", help="env var holding the isolated database DSN (with --apply)"
    )
    args = parser.parse_args(argv)

    ledger = args.ledger or Path(os.environ["ONEX_LEDGER_PATH"])
    archive = args.archive_dir or ledger.parent / "archive"
    try:
        plan = plan_history_import(
            read_ledger_history(ledger, archive), args.ledger_id, infer_shifts=True
        )
    except (OSError, ValueError, KeyError) as exc:
        sys.stderr.write(f"work_ledger_history_import: error: {exc!r}\n")
        return 2
    summary = _plan_summary(plan)
    if not args.apply:
        sys.stdout.write(json.dumps({"mode": "plan", **summary}, indent=2) + "\n")
        return 0
    if not args.dsn_env or args.dsn_env not in os.environ:
        sys.stderr.write(
            "work_ledger_history_import: error: --apply needs --dsn-env naming a set variable\n"
        )
        return 2
    dsn = os.environ[args.dsn_env]
    if not _loopback(dsn):
        sys.stderr.write(
            "work_ledger_history_import: refused: the history import writes only a loopback or "
            "local-socket database (an isolated compose project); never the dev lane.\n"
        )
        return 2
    try:
        report = asyncio.run(_run(dsn, plan))
    except (LedgerSeqChangedError, CitationConflictError) as exc:
        sys.stderr.write(f"work_ledger_history_import: rolled back: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(
            f"work_ledger_history_import: error: database write: {exc!r}\n"
        )
        return 2
    sys.stdout.write(
        json.dumps(
            {
                "mode": "apply",
                **summary,
                "report": json.loads(report.model_dump_json()),
            },
            indent=2,
        )
        + "\n"
    )
    return 0


__all__ = [
    "CITATION_TABLE",
    "CitationConflictError",
    "LedgerSeqChangedError",
    "apply_history_import",
    "backfill_main",
    "ledger_seq_fingerprint",
    "ordering_key",
    "plan_history_import",
]


if __name__ == "__main__":
    raise SystemExit(backfill_main())
