# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-ledger parity check (OMN-19513): the markdown ledger against the projection.

Run as ``python -m omnimarket.nodes.node_projection_work_ledger.parity``. It
reads the markdown ledger (and optionally its archive splits) for a window,
reads the projection (an exported JSON file, or the lab database through a DSN
named by an environment variable), and reports:

* rows in the file and missing from the projection,
* rows in the projection and missing from the file,
* state mismatches: the file's rows in the window folded with the SAME pure fold
  the projection applies, compared entity by entity on ``opened_at`` and
  ``closed_at`` (only fields the window's rows can establish).

Exit code 0 is exact parity (no mismatch and at least one row in the window), 1
is a mismatch or an empty window, 2 is an error. The retirement bar is exact
parity over one full UTC day.

Rows the emit path can never carry (a legacy or tool-internal type) are counted
apart, never as mismatches, and never silently dropped.

``--explain`` (OMN-20535) adds an ``explain`` object that classifies each row missing
from the projection by the evidence on the emitting host's state directory
(``--state-dir``, else ``ONEX_STATE_DIR``, else ``$OMNI_HOME/.onex_state``; none of
them is an error): ``journal-pending`` when the hook-emit journal still holds it,
``journal-dead-letter`` when the drainer's quarantine or its loss log
(``hook_emit_journal_losses.jsonl``) names it, ``failure-log`` when the dual write's
``work-ledger-emit-failures.jsonl`` names it, else ``unexplained``. Each evidence
source is reported with its path, or as ``absent:`` when it is not there, so a
missing source is never read as an empty one. The exit code is unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_projection_work_ledger.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    apply_ops,
    fold_row,
    parse_stamp,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityLossClass,
    EnumParityMismatchKind,
    ModelParityExplain,
    ModelParityExplainedRow,
    ModelParityExplainEvidence,
    ModelParityMismatch,
    ModelWorkLedgerParityReport,
)

FAILURE_LOG_NAME = "work-ledger-emit-failures.jsonl"
JOURNAL_DIR_NAME = "hook_emit_journal"
QUARANTINE_DIR_NAME = "quarantine"
LOSS_LOG_NAME = "hook_emit_journal_losses.jsonl"
_LEDGER_EVENT_PREFIX = "work.ledger."

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")
_TYPE_CELL = re.compile(r"^\S+ \| (?P<type>[^|]*?)\s*(?:\||$)")

_SELECT_ROWS = """
    SELECT row_id, row_ts FROM omninode_internal.work_ledger_rows
    WHERE row_ts >= $1 AND row_ts <= $2
"""
_SELECT_STATE = """
    SELECT entity_key, kind, opened_at, closed_at FROM omninode_internal.work_ledger_state
"""


def split_rows(text: str) -> list[str]:
    """Group a ledger's lines into rows: a stamp line plus its continuation lines."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if _ROW_START.match(line):
            rows.append([line])
        elif rows and line.strip():
            rows[-1].append(line)
    return ["\n".join(r).strip() for r in rows]


def _read_ledger_files(ledger: Path, archive_dir: Path | None) -> list[str]:
    paths = [ledger]
    if archive_dir is not None:
        paths = sorted(archive_dir.glob("ROLLING_WORK_LEDGER_*-split.md")) + paths
    rows: list[str] = []
    for path in paths:
        rows.extend(split_rows(path.read_text(encoding="utf-8")))
    return rows


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def compare(
    *,
    file_rows: list[str],
    projection_rows: dict[str, str],
    projection_state: dict[str, dict[str, Any]],
    since: datetime,
    until: datetime,
) -> ModelWorkLedgerParityReport:
    """Compare the file's rows in the window with the projection. Pure."""
    in_window: dict[str, str] = {}
    fold_results = []
    unemittable: Counter[str] = Counter()
    for raw in file_rows:
        stamp = parse_stamp(raw.split(" | ", 1)[0])
        if not since <= stamp <= until:
            continue
        try:
            result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
        except WorkLedgerFoldError:
            match = _TYPE_CELL.match(raw)
            unemittable[match.group("type") if match else "?"] += 1
            continue
        in_window[result.row.row_id] = raw
        fold_results.append(result)

    mismatches: list[ModelParityMismatch] = []
    for row_id in sorted(in_window.keys() - projection_rows.keys()):
        mismatches.append(
            ModelParityMismatch(
                kind=EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION,
                key=row_id,
                detail=in_window[row_id][:160],
            )
        )
    for row_id in sorted(projection_rows.keys() - in_window.keys()):
        mismatches.append(
            ModelParityMismatch(
                kind=EnumParityMismatchKind.ROW_MISSING_IN_FILE,
                key=row_id,
                detail=f"row_ts={projection_rows[row_id]}",
            )
        )

    expected: dict[str, dict[str, object]] = {}
    for result in fold_results:
        apply_ops(expected, result.ops)
    for key in sorted(expected):
        want = expected[key]
        have = projection_state.get(key)
        if have is None:
            mismatches.append(
                ModelParityMismatch(
                    kind=EnumParityMismatchKind.STATE_MISSING_IN_PROJECTION, key=key
                )
            )
            continue
        for field, kind in (
            ("opened_at", EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS),
            ("closed_at", EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS),
        ):
            if want[field] is not None and _iso(want[field]) != _iso(have.get(field)):
                mismatches.append(
                    ModelParityMismatch(
                        kind=kind,
                        key=key,
                        detail=f"file={_iso(want[field])} projection={_iso(have.get(field))}",
                    )
                )
    return ModelWorkLedgerParityReport(
        window_since=since,
        window_until=until,
        file_rows=len(in_window),
        projection_rows=len(projection_rows),
        unemittable_rows=sum(unemittable.values()),
        unemittable_types=dict(unemittable),
        state_entities_compared=len(expected),
        mismatches=tuple(mismatches),
    )


def _jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            entries.append(value)
    return entries


def _journal_row_ids(directory: Path) -> tuple[dict[str, str], int]:
    """``row_id`` -> file name for each work-ledger record directly under ``directory``,
    and how many record files could not be read (reported, never silently skipped)."""
    found: dict[str, str] = {}
    unreadable = 0
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".reason.json"):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(record, dict):
            continue
        event_type = record.get("event_type")
        payload = record.get("payload")
        if not (
            isinstance(event_type, str) and event_type.startswith(_LEDGER_EVENT_PREFIX)
        ):
            continue
        row_id = payload.get("row_id") if isinstance(payload, dict) else None
        if isinstance(row_id, str) and row_id:
            found[row_id] = path.name
    return found, unreadable


def load_explain_evidence(
    *,
    state_dir: Path,
    journal_dir: Path | None = None,
    loss_log_path: Path | None = None,
) -> ModelParityExplainEvidence:
    """Read the four evidence sources. Never raises on an absent source.

    The paths are the producers' own: the dual write's failure log under
    ``state_dir``; the journal at ``journal_dir`` (the drainer's
    ``ONEX_HOOK_EMIT_JOURNAL_DIR``), else ``state_dir/hook_emit_journal``; the
    drainer's loss log at ``loss_log_path`` (its ``ONEX_HOOK_EMIT_LOSS_LOG``), else
    beside the journal directory.
    """
    journal = journal_dir if journal_dir is not None else state_dir / JOURNAL_DIR_NAME
    quarantine = journal / QUARANTINE_DIR_NAME
    failure_log = state_dir / FAILURE_LOG_NAME
    loss_log = (
        loss_log_path if loss_log_path is not None else journal.parent / LOSS_LOG_NAME
    )
    sources: dict[str, str] = {}
    pending: dict[str, str] = {}
    dead_letter: dict[str, str] = {}
    failures: dict[str, str] = {}

    for name, path in (("journal", journal), ("quarantine", quarantine)):
        if not path.is_dir():
            sources[name] = f"absent:{path}"
            continue
        found, unreadable = _journal_row_ids(path)
        sources[name] = str(path) + (
            f" (unreadable={unreadable})" if unreadable else ""
        )
        for row_id, file_name in found.items():
            if name == "journal":
                pending[row_id] = f"queued in the journal as {file_name}"
            else:
                dead_letter[row_id] = (
                    f"dead-lettered to {QUARANTINE_DIR_NAME}/{file_name}"
                )

    if loss_log.is_file():
        sources["loss_log"] = str(loss_log)
        for entry in _jsonl(loss_log):
            lost_id = entry.get("row_id")
            if isinstance(lost_id, str) and lost_id:
                dead_letter.setdefault(
                    lost_id,
                    f"drainer loss log: {entry.get('disposition')} "
                    f"{entry.get('journal_file') or ''}".strip(),
                )
    else:
        sources["loss_log"] = f"absent:{loss_log}"

    if failure_log.is_file():
        sources["failure_log"] = str(failure_log)
        for entry in _jsonl(failure_log):
            skipped_id = entry.get("row_id")
            if isinstance(skipped_id, str) and skipped_id:
                failures.setdefault(
                    skipped_id,
                    f"dual write skipped it: {str(entry.get('reason', ''))[:160]}",
                )
    else:
        sources["failure_log"] = f"absent:{failure_log}"

    return ModelParityExplainEvidence(
        pending=pending, dead_letter=dead_letter, failure_log=failures, sources=sources
    )


def explain_missing(
    missing_row_ids: list[str], evidence: ModelParityExplainEvidence
) -> ModelParityExplain:
    """Classify each missing row. Pure.

    Precedence: a record still queued is pending whatever else is recorded about it; a
    drainer loss outranks a dual-write failure line, because the record reached the journal.
    """
    rows: list[ModelParityExplainedRow] = []
    for row_id in missing_row_ids:
        if row_id in evidence.pending:
            loss_class, detail = (
                EnumParityLossClass.JOURNAL_PENDING,
                evidence.pending[row_id],
            )
        elif row_id in evidence.dead_letter:
            loss_class = EnumParityLossClass.JOURNAL_DEAD_LETTER
            detail = evidence.dead_letter[row_id]
        elif row_id in evidence.failure_log:
            loss_class, detail = (
                EnumParityLossClass.FAILURE_LOG,
                evidence.failure_log[row_id],
            )
        else:
            loss_class, detail = EnumParityLossClass.UNEXPLAINED, ""
        rows.append(
            ModelParityExplainedRow(row_id=row_id, loss_class=loss_class, detail=detail)
        )
    counts = Counter(r.loss_class for r in rows)
    return ModelParityExplain(
        failure_log=counts[EnumParityLossClass.FAILURE_LOG],
        journal_dead_letter=counts[EnumParityLossClass.JOURNAL_DEAD_LETTER],
        journal_pending=counts[EnumParityLossClass.JOURNAL_PENDING],
        unexplained=counts[EnumParityLossClass.UNEXPLAINED],
        evidence=dict(evidence.sources),
        rows=tuple(rows),
    )


def _env_path(name: str) -> Path | None:
    value = os.environ.get(name)
    return Path(value) if value else None


def resolve_state_dir(explicit: Path | None) -> Path:
    """``--state-dir``, else ``ONEX_STATE_DIR``, else ``$OMNI_HOME/.onex_state``; else KeyError."""
    if explicit is not None:
        return explicit
    if os.environ.get("ONEX_STATE_DIR"):
        return Path(os.environ["ONEX_STATE_DIR"])
    if os.environ.get("OMNI_HOME"):
        return Path(os.environ["OMNI_HOME"]) / ".onex_state"
    raise KeyError(
        "--explain needs the emitting host's state directory: pass --state-dir, "
        "or set ONEX_STATE_DIR or OMNI_HOME"
    )


def load_projection_json(
    path: Path,
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Read an export: ``{"rows": [{row_id,row_ts}], "state": [{entity_key,...}]}``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = {str(r["row_id"]): str(r["row_ts"]) for r in data.get("rows", [])}
    state = {str(s["entity_key"]): dict(s) for s in data.get("state", [])}
    return rows, state


async def _load_projection_db(
    dsn: str, since: datetime, until: datetime
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    import asyncpg  # lazy: only the live read needs a database driver

    conn = await asyncpg.connect(dsn)
    try:
        rows = {
            r["row_id"]: _iso(r["row_ts"]) or ""
            for r in await conn.fetch(_SELECT_ROWS, since, until)
        }
        state = {r["entity_key"]: dict(r) for r in await conn.fetch(_SELECT_STATE)}
    finally:
        await conn.close()
    return rows, state


def _parse_when(value: str) -> datetime:
    return parse_stamp(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else ""
    )
    parser.add_argument(
        "--ledger", type=Path, help="markdown ledger (default: $ONEX_LEDGER_PATH)"
    )
    parser.add_argument(
        "--archive-dir", type=Path, help="directory holding the archive splits"
    )
    parser.add_argument(
        "--since", required=True, type=_parse_when, help="UTC, YYYY-MM-DDTHH:MM:SSZ"
    )
    parser.add_argument("--until", type=_parse_when, help="UTC; default now")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--projection-json", type=Path, help="exported projection JSON")
    source.add_argument(
        "--dsn-env", help="name of the env var holding the lab database DSN"
    )
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument(
        "--explain",
        action="store_true",
        help="classify each row missing from the projection (OMN-20535)",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="the emitting host's state directory for --explain "
        "(default: $ONEX_STATE_DIR, else $OMNI_HOME/.onex_state)",
    )
    args = parser.parse_args(argv)

    ledger = args.ledger or Path(os.environ["ONEX_LEDGER_PATH"])
    until = args.until or datetime.now(UTC).replace(microsecond=0)
    try:
        file_rows = _read_ledger_files(ledger, args.archive_dir)
        if args.projection_json is not None:
            proj_rows, proj_state = load_projection_json(args.projection_json)
        else:
            proj_rows, proj_state = asyncio.run(
                _load_projection_db(os.environ[args.dsn_env], args.since, until)
            )
        report = compare(
            file_rows=file_rows,
            projection_rows=proj_rows,
            projection_state=proj_state,
            since=args.since,
            until=until,
        )
        if args.explain:
            missing = [
                m.key
                for m in report.mismatches
                if m.kind is EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION
            ]
            evidence = load_explain_evidence(
                state_dir=resolve_state_dir(args.state_dir),
                journal_dir=_env_path("ONEX_HOOK_EMIT_JOURNAL_DIR"),
                loss_log_path=_env_path("ONEX_HOOK_EMIT_LOSS_LOG"),
            )
            report = report.model_copy(
                update={"explain": explain_missing(missing, evidence)}
            )
    except (OSError, KeyError, ValueError) as exc:
        sys.stderr.write(f"work_ledger_parity: error: {exc!r}\n")
        return 2

    if args.format == "json":
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(
            f"window {report.window_since:%Y-%m-%dT%H:%M:%SZ}..{report.window_until:%Y-%m-%dT%H:%M:%SZ} "
            f"file_rows={report.file_rows} projection_rows={report.projection_rows} "
            f"unemittable={report.unemittable_rows} entities={report.state_entities_compared} "
            f"mismatches={len(report.mismatches)} exact={report.exact}\n"
        )
        for m in report.mismatches[:50]:
            sys.stdout.write(f"  {m.kind.value} {m.key} {m.detail}\n")
        if report.explain is not None:
            e = report.explain
            sys.stdout.write(
                f"explain failure-log={e.failure_log} journal-dead-letter={e.journal_dead_letter} "
                f"journal-pending={e.journal_pending} unexplained={e.unexplained}\n"
            )
    return 0 if report.exact else 1


if __name__ == "__main__":
    raise SystemExit(main())
