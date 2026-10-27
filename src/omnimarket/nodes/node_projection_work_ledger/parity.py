# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Work-ledger parity check (OMN-19513): the markdown ledger against the projection.

Run as ``python -m omnimarket.nodes.node_projection_work_ledger.parity``. It
reads the markdown ledger (and optionally its archive splits) for a window,
reads the projection (an exported JSON file, or the lab database through a DSN
named by an environment variable), and reports:

* rows in the file and missing from the projection,
* rows in the projection and missing from the file,
* state mismatches: the file's rows through the state horizon folded with the SAME pure fold
  the projection applies, compared entity by entity on ``opened_at`` and
  ``closed_at`` (only entities touched by the window's rows).

State is compared as of ``--state-horizon`` (default: the file's latest row),
folding from the window's start through that horizon. A projected timestamp
strictly after the horizon is excluded only with the projection's own later row:
the row id that set the field must be stamped after the horizon at that time,
and must not be among the file's rows folded for state. Its entity is then
counted apart in ``state_after_horizon``, without a mismatch for that field.

Exit code 0 is exact parity (no mismatch and at least one row in the window), 1
is a mismatch or an empty window, 2 is an error. The retirement bar is exact
parity over one full UTC day.

Rows the emit path can never carry (a legacy or tool-internal type) are counted
apart, never as mismatches, and never silently dropped.

OMN-21107 TA.5 compares projected ``raw_row`` text and parsed authority fields
for every shared row in the window. Missing projected text is a mismatch;
metadata-only exports cannot establish exact parity. It also compares
the relationship closure derived from each side's rows through the state
horizon, restricted to entities touched by the window.

``--explain`` (OMN-20535) adds an ``explain`` object that classifies each row missing
from the projection by the evidence on the emitting host's state directory
(``--state-dir``, else ``ONEX_STATE_DIR``, else ``$OMNI_HOME/.onex_state``; none of
them is an error): ``journal-pending`` when the hook-emit journal still holds it,
``journal-dead-letter`` when the drainer's quarantine or its loss log
(``hook_emit_journal_losses.jsonl``) names it, ``failure-log`` when the dual write's
``work-ledger-emit-failures.jsonl`` names it, else ``unexplained``. Each evidence
source is reported with its path, or as ``absent:`` when it is not there, so a
missing source is never read as an empty one. The exit code is unchanged.

``--utc-day YYYY-MM-DD`` (OMN-20536) sets the window to that whole UTC day, and
``--receipt`` (which needs it, and implies ``--explain``) prints the daily parity
receipt instead of the report: one STATUS ledger row, ``lane=work-ledger-parity``,
carrying ``file_rows``, ``projection_rows``, ``missing``, ``extra``,
``state_mismatches``, ``text_mismatches``, ``unexplained``, ``backfilled`` and
``exact`` for that day.
``backfilled`` is 1 when any of the day's projected rows carries the source
``onex-ledger-emit-backfill`` or ``ledger-emit``; ``exact=yes`` needs every count at
zero, ``backfilled=0`` and at least one row. The receipt's exit code is 0 for an
exact day and 1 otherwise, so a caller appends the row on 0 or 1 and on nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from omnimarket.handlers.work_ledger_fold import (
    WorkLedgerFoldError,
    apply_ops,
    close_relationships,
    fold_row,
    links_for_rows,
    parse_stamp,
)
from omnimarket.handlers.work_ledger_text import split_ledger_rows
from omnimarket.models.model_work_ledger_fold_request import (
    ModelWorkLedgerFoldRequest,
)
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerLink
from omnimarket.nodes.node_projection_work_ledger.models.model_work_ledger_parity_report import (
    EnumParityLossClass,
    EnumParityMismatchKind,
    ModelParityExplain,
    ModelParityExplainedRow,
    ModelParityExplainEvidence,
    ModelParityMismatch,
    ModelWorkLedgerParityReport,
)
from omnimarket.nodes.node_work_ledger_emit_effect.handlers.row_parser import (
    LedgerRowRefusalError,
    parse_ledger_row,
)

FAILURE_LOG_NAME = "work-ledger-emit-failures.jsonl"
JOURNAL_DIR_NAME = "hook_emit_journal"
QUARANTINE_DIR_NAME = "quarantine"
LOSS_LOG_NAME = "hook_emit_journal_losses.jsonl"
_LEDGER_EVENT_PREFIX = "work.ledger."
BACKFILL_SOURCE = "onex-ledger-emit-backfill"
BACKFILL_SOURCES = frozenset({"onex-ledger-emit-backfill", "ledger-emit"})
RECEIPT_LANE = "work-ledger-parity"
RECEIPT_ACTOR = "script:work-ledger-parity"

_TYPE_CELL = re.compile(r"^\S+ \| (?P<type>[^|]*?)\s*(?:\||$)")

_SELECT_ROWS = """
    SELECT row_id, row_ts FROM omninode_internal.work_ledger_rows
    WHERE row_ts >= $1 AND row_ts <= $2
"""
_SELECT_SOURCES = """
    SELECT source, count(*) AS n FROM omninode_internal.work_ledger_rows
    WHERE row_ts >= $1 AND row_ts <= $2 GROUP BY source
"""
_SELECT_STATE = """
    SELECT entity_key, kind, opened_at, closed_at, opened_row_id, closed_row_id
    FROM omninode_internal.work_ledger_state
"""
_SELECT_LATER_ROWS = """
    SELECT row_id, row_ts FROM omninode_internal.work_ledger_rows WHERE row_ts > $1
"""
_SELECT_RAW_ROWS = """
    SELECT row_id, raw_row FROM omninode_internal.work_ledger_rows
    WHERE row_ts >= $1 AND row_ts <= $2
"""


def _read_ledger_files(ledger: Path, archive_dir: Path | None) -> list[str]:
    paths = [ledger]
    if archive_dir is not None:
        paths = sorted(archive_dir.glob("ROLLING_WORK_LEDGER_*-split.md")) + paths
    rows: list[str] = []
    for path in paths:
        rows.extend(split_ledger_rows(path.read_text(encoding="utf-8")))
    return rows


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return str(value)


def file_horizon(file_rows: list[str], until: datetime) -> datetime:
    """The latest first-cell timestamp in the file, or ``until`` when empty. Pure."""
    return max(
        (parse_stamp(raw.split(" | ", 1)[0]) for raw in file_rows), default=until
    )


def _authority(raw: str) -> dict[str, object]:
    event = parse_ledger_row(raw)
    fields: dict[str, object] = {
        "type": event.row_type.value,
        "lane": event.row_lane,
        "tickets": event.tickets,
        "kind": next((cell.value for cell in event.cells if cell.key == "kind"), None),
    }
    fields.update(
        (name, getattr(event, name, None))
        for name in (
            "supersedes",
            "amends",
            "extends",
            "corrects",
            "operator_span",
            "prompt_id",
            "covers",
            "confirms",
        )
    )
    return fields


def _relation_state(
    state: Mapping[str, Mapping[str, object]],
) -> dict[str, dict[str, object]]:
    normalized: dict[str, dict[str, object]] = {}
    for key, entity in state.items():
        stamps: dict[str, datetime | None] = {}
        for field in ("opened_at", "closed_at"):
            value = entity.get(field)
            if isinstance(value, str):
                value = datetime.fromisoformat(value)
            if isinstance(value, datetime):
                stamps[field] = (
                    value.replace(tzinfo=UTC)
                    if value.tzinfo is None
                    else value.astimezone(UTC)
                )
            else:
                stamps[field] = None
        opened, closed = stamps["opened_at"], stamps["closed_at"]
        normalized[key] = {
            **stamps,
            "until_at": None,
            "is_open": opened is not None and (closed is None or closed < opened),
        }
    return normalized


def compare(
    *,
    file_rows: list[str],
    projection_rows: dict[str, str],
    projection_state: dict[str, dict[str, Any]],
    since: datetime,
    until: datetime,
    state_horizon: datetime | None = None,
    later_projection_rows: Mapping[str, str] | None = None,
    projection_raw_rows: Mapping[str, str] | None = None,
) -> ModelWorkLedgerParityReport:
    """Compare the file's rows in the window with the projection. Pure."""
    # Missing evidence must never disable the authority checks.
    projection_raw_rows = projection_raw_rows if projection_raw_rows is not None else {}
    stamped_rows = [(parse_stamp(raw.split(" | ", 1)[0]), raw) for raw in file_rows]
    if state_horizon is None:
        state_horizon = file_horizon(file_rows, until)
    in_window: dict[str, str] = {}
    file_text_rows: dict[str, str] = {}
    file_links: list[ModelWorkLedgerLink] = []
    state_row_ids: set[str] = set()
    expected: dict[str, dict[str, object]] = {}
    touched: set[str] = set()
    unemittable: Counter[str] = Counter()
    for stamp, raw in stamped_rows:
        row_in_window = since <= stamp <= until
        row_in_state = since <= stamp <= state_horizon
        if not (row_in_window or row_in_state):
            continue
        try:
            result = fold_row(ModelWorkLedgerFoldRequest(raw_row=raw))
        except WorkLedgerFoldError:
            if row_in_window:
                match = _TYPE_CELL.match(raw)
                unemittable[match.group("type") if match else "?"] += 1
            continue
        if row_in_window:
            in_window[result.row.row_id] = raw
            file_text_rows[result.row.row_id] = result.row.raw_row
            touched.update(op.entity_key for op in result.ops)
        if row_in_state:
            state_row_ids.add(result.row.row_id)
            apply_ops(expected, result.ops)
            file_links.extend(result.links)

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

    rows_text_compared = 0
    for row_id in sorted(in_window.keys() & projection_rows.keys()):
        if row_id not in projection_raw_rows:
            mismatches.append(
                ModelParityMismatch(
                    kind=EnumParityMismatchKind.ROW_TEXT_MISSING_IN_PROJECTION,
                    key=row_id,
                )
            )
            continue
        rows_text_compared += 1
        file_text = file_text_rows[row_id]
        projection_text = projection_raw_rows[row_id]
        if file_text == projection_text:
            continue
        offset = next(
            (
                i
                for i, (a, b) in enumerate(
                    zip(file_text, projection_text, strict=False)
                )
                if a != b
            ),
            min(len(file_text), len(projection_text)),
        )
        mismatches.append(
            ModelParityMismatch(
                kind=EnumParityMismatchKind.ROW_TEXT_DIFFERS,
                key=row_id,
                detail=f"first difference at offset {offset}; file_len={len(file_text)} projection_len={len(projection_text)}",
            )
        )
        try:
            projected_authority = _authority(projection_text)
        except LedgerRowRefusalError:
            detail = "projection row does not parse"
        else:
            file_authority = _authority(file_text)
            differing = sorted(
                name
                for name in file_authority
                if file_authority[name] != projected_authority[name]
            )
            if not differing:
                continue
            detail = f"fields={','.join(differing)}"
        mismatches.append(
            ModelParityMismatch(
                kind=EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS,
                key=row_id,
                detail=detail,
            )
        )

    expected = {key: value for key, value in expected.items() if key in touched}
    state_after_horizon: set[str] = set()
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
        for field, row_id_field, kind in (
            (
                "opened_at",
                "opened_row_id",
                EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS,
            ),
            (
                "closed_at",
                "closed_row_id",
                EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS,
            ),
        ):
            if want[field] is None:
                continue
            projected = have.get(field)
            if isinstance(projected, str):
                projected = datetime.fromisoformat(projected)
            if isinstance(projected, datetime):
                projected = projected.astimezone(UTC)
                projected_row_id = have.get(row_id_field)
                if (
                    projected > state_horizon
                    and later_projection_rows
                    and isinstance(projected_row_id, str)
                    and projected_row_id in later_projection_rows
                    and later_projection_rows[projected_row_id] == _iso(projected)
                    and projected_row_id not in state_row_ids
                ):
                    state_after_horizon.add(key)
                    continue
            if _iso(want[field]) != _iso(projected):
                mismatches.append(
                    ModelParityMismatch(
                        kind=kind,
                        key=key,
                        detail=f"file={_iso(want[field])} projection={_iso(have.get(field))}",
                    )
                )
    relations_compared = 0
    file_closure, _ = close_relationships(_relation_state(expected), file_links)
    projection_links = (
        link
        for link in links_for_rows(projection_raw_rows.values())
        if since <= link.at <= state_horizon
    )
    projection_closure, _ = close_relationships(
        _relation_state(
            {key: projection_state[key] for key in expected if key in projection_state}
        ),
        projection_links,
    )
    for key in sorted(expected):
        if key not in projection_closure:
            continue
        relations_compared += 1
        differences: list[str] = []
        for field in (
            "superseded_by",
            "amended_by",
            "extended_by",
            "corrected_by",
            "operator_spans",
        ):
            file_value = list(cast(tuple[object, ...], file_closure[key][field]))
            projection_value = list(
                cast(tuple[object, ...], projection_closure[key][field])
            )
            if file_value != projection_value:
                differences.append(
                    f"{field}: file={file_value} projection={projection_value}"
                )
        if differences:
            mismatches.append(
                ModelParityMismatch(
                    kind=EnumParityMismatchKind.STATE_RELATIONS_DIFFER,
                    key=key,
                    detail="; ".join(differences),
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
        rows_text_compared=rows_text_compared,
        relations_compared=relations_compared,
        state_horizon=state_horizon,
        state_after_horizon=tuple(sorted(state_after_horizon)),
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
    path: Path, since: datetime, until: datetime
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Read exported rows in the inclusive window, and the full exported state.

    Match the database loader's row filter. Parse each full timestamp before
    filtering: an invalid export must fail rather than produce a parity receipt.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = {
        str(r["row_id"]): str(r["row_ts"])
        for r in data.get("rows", [])
        if since <= parse_stamp(str(r["row_ts"])) <= until
    }
    state = {str(s["entity_key"]): dict(s) for s in data.get("state", [])}
    return rows, state


def load_projection_later_rows_json(path: Path, horizon: datetime) -> dict[str, str]:
    """Read exported row evidence strictly after the state horizon, in UTC."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(r["row_id"]): _iso(stamp) or ""
        for r in data.get("rows", [])
        if (stamp := parse_stamp(str(r["row_ts"]))) > horizon
    }


def load_projection_raw_rows_json(
    path: Path, since: datetime, end: datetime
) -> dict[str, str]:
    """Read available exported raw text over the inclusive state and row range."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        str(r["row_id"]): r["raw_row"]
        for r in data.get("rows", [])
        if since <= parse_stamp(str(r["row_ts"])) <= end
        and isinstance(r.get("raw_row"), str)
    }


def load_projection_sources_json(
    path: Path, since: datetime, until: datetime
) -> dict[str, int]:
    """Projected rows in the window per ``source`` (absent counts as ``unknown``)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    counts: Counter[str] = Counter()
    for r in data.get("rows", []):
        if since <= parse_stamp(str(r["row_ts"])[:20]) <= until:
            counts[str(r.get("source") or "unknown")] += 1
    return dict(counts)


async def _load_projection_sources_db(
    dsn: str, since: datetime, until: datetime
) -> dict[str, int]:
    import asyncpg  # lazy: only the live read needs a database driver

    conn = await asyncpg.connect(dsn)
    try:
        return {
            str(r["source"]): int(r["n"])
            for r in await conn.fetch(_SELECT_SOURCES, since, until)
        }
    finally:
        await conn.close()


def backfilled_rows(report: ModelWorkLedgerParityReport) -> int:
    """Count projected rows emitted through either backfill path."""
    return sum(
        count
        for source, count in report.projection_sources.items()
        if source in BACKFILL_SOURCES
    )


def receipt_exact(report: ModelWorkLedgerParityReport) -> bool:
    """Phase 1's bar: exact parity and no backfilled row in the window."""
    return report.exact and backfilled_rows(report) == 0


def receipt_row(report: ModelWorkLedgerParityReport, day: str, now: datetime) -> str:
    """The daily parity receipt for ``day``: one STATUS ledger row. Pure."""
    kinds = Counter(m.kind for m in report.mismatches)
    missing = kinds[EnumParityMismatchKind.ROW_MISSING_IN_PROJECTION]
    extra = kinds[EnumParityMismatchKind.ROW_MISSING_IN_FILE]
    state = sum(
        kinds[k]
        for k in (
            EnumParityMismatchKind.STATE_MISSING_IN_PROJECTION,
            EnumParityMismatchKind.STATE_OPENED_AT_DIFFERS,
            EnumParityMismatchKind.STATE_CLOSED_AT_DIFFERS,
            EnumParityMismatchKind.STATE_RELATIONS_DIFFER,
        )
    )
    text = sum(
        kinds[k]
        for k in (
            EnumParityMismatchKind.ROW_TEXT_DIFFERS,
            EnumParityMismatchKind.ROW_TEXT_MISSING_IN_PROJECTION,
            EnumParityMismatchKind.ROW_AUTHORITY_DIFFERS,
        )
    )
    unexplained = report.explain.unexplained if report.explain is not None else missing
    backfilled = 1 if backfilled_rows(report) > 0 else 0
    exact = "yes" if receipt_exact(report) else "no"
    cells = [
        f"{now:%Y-%m-%dT%H:%M:%SZ}", "STATUS", f"lane={RECEIPT_LANE}",
        f"actor={RECEIPT_ACTOR}", "model=none", f"day={day}",
        f"file_rows={report.file_rows}", f"projection_rows={report.projection_rows}",
        f"missing={missing}", f"extra={extra}", f"state_mismatches={state}",
        f"text_mismatches={text}",
        f"unexplained={unexplained}", f"backfilled={backfilled}", f"exact={exact}",
        f"Work-ledger parity receipt for UTC day {day}",
    ]  # fmt: skip
    return " | ".join(cells)


async def _load_projection_db(
    dsn: str, since: datetime, until: datetime, horizon: datetime
) -> tuple[dict[str, str], dict[str, dict[str, Any]], dict[str, str], dict[str, str]]:
    import asyncpg  # lazy: only the live read needs a database driver

    conn = await asyncpg.connect(dsn)
    try:
        rows = {
            r["row_id"]: _iso(r["row_ts"]) or ""
            for r in await conn.fetch(_SELECT_ROWS, since, until)
        }
        state = {r["entity_key"]: dict(r) for r in await conn.fetch(_SELECT_STATE)}
        later_rows = {
            r["row_id"]: _iso(r["row_ts"]) or ""
            for r in await conn.fetch(_SELECT_LATER_ROWS, horizon)
        }
        raw_rows = {
            r["row_id"]: r["raw_row"]
            for r in await conn.fetch(_SELECT_RAW_ROWS, since, max(until, horizon))
        }
    finally:
        await conn.close()
    return rows, state, later_rows, raw_rows


def _window(
    utc_day: str | None, since: datetime | None, until: datetime | None
) -> tuple[datetime, datetime]:
    """The checked window: the whole of ``utc_day``, else ``since`` to ``until`` (or now)."""
    if utc_day is not None:
        start = parse_stamp(f"{utc_day}T00:00:00Z")
        return start, start + timedelta(days=1, seconds=-1)
    if since is None:
        raise ValueError("no window: neither --utc-day nor --since")
    return since, until or datetime.now(UTC).replace(microsecond=0)


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
    parser.add_argument("--since", type=_parse_when, help="UTC, YYYY-MM-DDTHH:MM:SSZ")
    parser.add_argument(
        "--utc-day", help="YYYY-MM-DD: the window is that whole UTC day (OMN-20536)"
    )
    parser.add_argument(
        "--receipt",
        action="store_true",
        help="print the daily parity receipt row for --utc-day (implies --explain)",
    )
    parser.add_argument("--until", type=_parse_when, help="UTC; default now")
    parser.add_argument(
        "--state-horizon",
        type=_parse_when,
        help="UTC; the latest file row folded for the state comparison (default: the file's latest row)",
    )
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

    if args.receipt and args.utc_day is None:
        sys.stderr.write("work_ledger_parity: error: --receipt needs --utc-day\n")
        return 2
    if args.utc_day is None and args.since is None:
        parser.error("one of --since or --utc-day is required")
    try:
        since, until = _window(args.utc_day, args.since, args.until)
    except ValueError:
        sys.stderr.write(
            f"work_ledger_parity: error: --utc-day must be YYYY-MM-DD, got {args.utc_day!r}\n"
        )
        return 2
    ledger = args.ledger or Path(os.environ["ONEX_LEDGER_PATH"])
    try:
        file_rows = _read_ledger_files(ledger, args.archive_dir)
        horizon = args.state_horizon or file_horizon(file_rows, until)
        if args.projection_json is not None:
            proj_rows, proj_state = load_projection_json(
                args.projection_json, since, until
            )
            later_rows = load_projection_later_rows_json(args.projection_json, horizon)
            raw_rows = load_projection_raw_rows_json(
                args.projection_json, since, max(until, horizon)
            )
            sources = load_projection_sources_json(args.projection_json, since, until)
        else:
            dsn = os.environ[args.dsn_env]
            try:
                proj_rows, proj_state, later_rows, raw_rows = asyncio.run(
                    _load_projection_db(dsn, since, until, horizon)
                )
                sources = asyncio.run(_load_projection_sources_db(dsn, since, until))
            except Exception as exc:
                # Driver errors are not all OSError subclasses. Keep this boundary
                # around database I/O only: exit 1 is reserved for a parity result,
                # and a failed read must never produce a report or receipt.
                sys.stderr.write(f"work_ledger_parity: error: database read: {exc!r}\n")
                return 2
        report = compare(
            file_rows=file_rows,
            projection_rows=proj_rows,
            projection_state=proj_state,
            since=since,
            until=until,
            state_horizon=horizon,
            later_projection_rows=later_rows,
            projection_raw_rows=raw_rows,
        ).model_copy(update={"projection_sources": sources})
        if args.explain or args.receipt:
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

    if args.receipt:
        sys.stdout.write(receipt_row(report, args.utc_day, datetime.now(UTC)) + "\n")
        return 0 if receipt_exact(report) else 1
    if args.format == "json":
        sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    else:
        sys.stdout.write(
            f"window {report.window_since:%Y-%m-%dT%H:%M:%SZ}..{report.window_until:%Y-%m-%dT%H:%M:%SZ} "
            f"file_rows={report.file_rows} projection_rows={report.projection_rows} "
            f"unemittable={report.unemittable_rows} entities={report.state_entities_compared} "
            f"state_after_horizon={len(report.state_after_horizon)} "
            f"mismatches={len(report.mismatches)} exact={report.exact} "
            f"text_rows={report.rows_text_compared} relations={report.relations_compared}\n"
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
