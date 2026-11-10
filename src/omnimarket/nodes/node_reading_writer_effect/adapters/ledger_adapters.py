# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ledger ports of node_reading_writer_effect: in memory by default, real backends by overlay.

The route is never a literal: the overlay names the environment variable that holds the ledger
file path, the read command or the append command, and a variable that is unset refuses naming
itself. The in-memory ledger is the default for tests and implements both ports.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from functools import singledispatch
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid5

from omnimarket.handlers import ledger_row_grammar as grammar
from omnimarket.models.model_reading import ModelLedgerEvidenceRow
from omnimarket.nodes.node_reading_writer_effect.overlay import (
    ModelCommandLedgerReadRoute,
    ModelFileLedgerReadRoute,
    ModelLedgerAppendRoute,
    ModelLedgerReadRoute,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelAppendOutcome,
    ModelLedgerQuery,
    ModelLedgerRead,
    ProtocolLedgerBatchRowSource,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)


class LedgerRouteError(RuntimeError):
    """A route the overlay names is not configured; the message names the variable."""


def _parse_time(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


def evidence_row_from_text(
    text: str,
    ingested_at: datetime | None = None,
    *,
    types: frozenset[str] | None = None,
) -> ModelLedgerEvidenceRow | None:
    """One ledger row line as evidence, or None when the line is not a row.

    With ``types``, a row of any other type is None before its cells are split and its fields indexed (OMN-23081): a
    reader that wants a few row types of a ledger of hundreds of thousands of lines pays for those rows alone.
    """
    row = grammar.parse_row(text, types=types)
    if row is None:
        return None
    cells = {
        key: values[0] for key, values in row.fields.items() if values and values[0]
    }
    return ModelLedgerEvidenceRow(
        stamp=_parse_time(row.stamp),
        row_type=row.row_type,
        lane=cells.get("lane"),
        cells=cells,
        raw=text.rstrip("\n"),
        ingested_at=ingested_at,
    )


def _matches(row: ModelLedgerEvidenceRow, query: ModelLedgerQuery) -> bool:
    if not (query.since <= row.stamp < query.until):
        return False
    if row.row_type != query.row_type:
        return False
    if query.lane is not None and row.lane != query.lane:
        return False
    return query.term is None or query.term in row.raw


class InMemoryLedger:
    """Both ports over a list of lines. ``read_error`` and ``append_status`` inject faults."""

    def __init__(self, lines: Sequence[str] = ()) -> None:
        self.lines: list[str] = list(lines)
        self.read_error: str | None = None
        self.append_status: str = "accepted"
        self.append_requests: dict[str, str] = {}

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        if self.read_error is not None:
            return ModelLedgerRead(error=self.read_error)
        rows = [
            row
            for line in self.lines
            if (row := evidence_row_from_text(line)) is not None
            and _matches(row, query)
        ]
        return ModelLedgerRead(rows=tuple(rows))

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        if self.append_status not in ("accepted", "duplicate"):
            return ModelAppendOutcome(
                status=self.append_status,
                message="injected append fault",
            )
        if request_id in self.append_requests:
            return ModelAppendOutcome(status="duplicate", message=request_id)
        self.append_requests[request_id] = rows
        self.lines.extend(line for line in rows.splitlines() if line.strip())
        return ModelAppendOutcome(status="accepted", message=request_id)


_ROLL_DAY_RE = re.compile(r"_(\d{4}-\d{2}-\d{2})-split\.md$")
_STAMP_TEXT_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _roll_ends_before(path: Path, since: datetime) -> bool:
    """True when a roll was cut more than a day before ``since``: it holds no row of the window (OMN-23081).

    A roll ``<name>_<day>-split.md`` is named for the day it was cut and holds rows no newer than that. The day is the
    cutting host's local day, so a row can be stamped up to a day later in UTC: only a roll named more than one day
    before ``since`` is passed over. A name that carries no day is never passed over. The name, unlike the file's
    modification time, survives a copy or a restore of the ledger.
    """
    match = _ROLL_DAY_RE.search(path.name)
    if match is None:
        return False
    named = datetime.strptime(match.group(1), "%Y-%m-%d").replace(tzinfo=UTC)
    return named + timedelta(days=1) < since


class FileLedgerRowSource:
    """Reads the live ledger file and the rolls beside it that can hold rows of the window."""

    def __init__(self, live: Path, archive_dir: Path | None) -> None:
        self._live = live
        self._archive_dir = archive_dir

    def _files(self, since: datetime) -> list[Path]:
        files = [self._live]
        if self._archive_dir is not None and self._archive_dir.is_dir():
            files.extend(
                sorted(
                    path
                    for path in self._archive_dir.iterdir()
                    if path.is_file()
                    and datetime.fromtimestamp(path.stat().st_mtime, UTC) >= since
                    and not _roll_ends_before(path, since)
                )
            )
        return files

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return self.read_many((query,))[0]

    def read_many(
        self, queries: Sequence[ModelLedgerQuery]
    ) -> tuple[ModelLedgerRead, ...]:
        """Answer every query from one pass over the files (OMN-23081).

        Each file is opened once whatever the number of queries, only the lines of a wanted row type are parsed, and a
        parsed row is offered to each query of its type. Each answer holds what a read of that query alone would hold,
        in the same order. A file that cannot be read fails every query, with the reason a single read gives. A roll cut
        more than a day before the earliest window is not opened, and a line stamped before it is not parsed (OMN-23081).
        """
        if not queries:
            return ()
        wanted: dict[str, list[int]] = {}
        for index, query in enumerate(queries):
            wanted.setdefault(query.row_type, []).append(index)
        types = frozenset(wanted)
        # A row answers a query of a lane only if the lane's text is in its line: lines naming none of the wanted lanes
        # are passed over before the row grammar runs. A query with no lane wants every line of its type.
        lanes = {query.lane for query in queries}
        needles = (
            tuple(sorted(x for x in lanes if x))
            if None not in lanes and any(lanes)
            else ()
        )
        found: list[list[ModelLedgerEvidenceRow]] = [[] for _ in queries]
        earliest = min(query.since for query in queries)
        # A row line starts with its 20-character UTC stamp, which sorts as time does: a line stamped before every query's
        # window is passed over before the grammar runs (a line that does not start with a stamp is no row either way).
        not_before = earliest.astimezone(UTC).strftime(_STAMP_TEXT_FMT)
        try:
            for path in self._files(earliest):
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        if line[:20] < not_before:
                            continue
                        if needles and not any(needle in line for needle in needles):
                            continue
                        row = evidence_row_from_text(line, types=types)
                        if row is None:
                            continue
                        for index in wanted[row.row_type]:
                            if _matches(row, queries[index]):
                                found[index].append(row)
        except OSError as exc:
            failed = ModelLedgerRead(
                error=f"ledger-file-unreadable:{type(exc).__name__}"
            )
            return tuple(failed for _ in queries)
        return tuple(ModelLedgerRead(rows=tuple(rows)) for rows in found)


def read_queries(
    source: ProtocolLedgerRowSource, queries: Sequence[ModelLedgerQuery]
) -> tuple[ModelLedgerRead, ...]:
    """Every query's answer, from one pass when the source has a single-pass read, else one read per query."""
    if isinstance(source, ProtocolLedgerBatchRowSource):
        return source.read_many(queries)
    return tuple(source.read(query) for query in queries)


def wire_correlation_id(correlation_id: str) -> str:
    """The form a run's correlation id takes on the read command's wire: the reader's request type is a UUID.

    An id that already reads as a UUID (dashed or 32 hex digits) is the same id, written dashed. Any other id is mapped to a
    UUID by a fixed rule (version 5), so the same run id is the same wire id every time and can be traced back by the rule.
    """
    try:
        return str(UUID(correlation_id))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, f"onex:correlation:{correlation_id}"))


class CommandLedgerRowSource:
    """Reads through the configured read command: one typed query JSON in, one answer out."""

    def __init__(
        self, argv: Sequence[str], timeout_seconds: int, row_limit: int
    ) -> None:
        self._argv = list(argv)
        self._timeout = timeout_seconds
        self._limit = row_limit

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        request: dict[str, object] = {
            "query": "rows",
            "since": query.since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "until": query.until.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "kinds": [query.row_type],
            "limit": self._limit,
        }
        if query.lane is not None:
            request["lane"] = query.lane
        if query.term is not None:
            request["term"] = query.term
        if query.correlation_id is not None:
            request["correlation_id"] = wire_correlation_id(query.correlation_id)
        try:
            done = subprocess.run(
                self._argv,
                input=json.dumps(request),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ModelLedgerRead(error=f"read-command-failed:{type(exc).__name__}")
        if done.returncode != 0:
            # The read CLI prints `Work-ledger read refused (<ExceptionClass>)`; keep the class so an
            # intermittent exit names its own cause (OMN-22775). Nothing else of stderr is kept.
            refused = _READ_REFUSED.search(done.stderr)
            suffix = f":{refused.group(1)}" if refused else ""
            return ModelLedgerRead(error=f"read-command-exit-{done.returncode}{suffix}")
        try:
            answer = json.loads(done.stdout)
        except json.JSONDecodeError:
            return ModelLedgerRead(error="read-answer-not-json")
        if (
            not isinstance(answer, dict)
            or answer.get("status", "ok") != "ok"
            or answer.get("error")
        ):
            return ModelLedgerRead(error="read-answer-error")
        raw_rows = answer.get("rows")
        if not isinstance(raw_rows, list):
            return ModelLedgerRead(error="read-answer-without-rows")
        matched = answer.get("matched")
        if answer.get("next_cursor") or (
            isinstance(matched, int) and matched > len(raw_rows)
        ):
            return ModelLedgerRead(error="read-answer-truncated")
        rows: list[ModelLedgerEvidenceRow] = []
        for item in raw_rows:
            if not isinstance(item, dict):
                return ModelLedgerRead(error="read-answer-malformed-row")
            text = item.get("raw_row")
            projected = item.get("projected_at")
            if not isinstance(text, str):
                return ModelLedgerRead(error="read-answer-malformed-row")
            row = evidence_row_from_text(
                text, _parse_time(projected) if isinstance(projected, str) else None
            )
            if row is not None and _matches(row, query):
                rows.append(row)
        return ModelLedgerRead(rows=tuple(rows))


_READ_REFUSED = re.compile(r"Work-ledger read refused \(([A-Za-z_][A-Za-z0-9_]*)\)")
_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| [^|\r\n]+ \| ")


def stamp_request_cell(rows: str, request_id: str) -> str:
    """Every row of ``rows`` with ``req=<request_id>`` as its last pipe cell.

    The bus append refuses a row that does not carry the whole pipe cell ``req=<id>`` of its
    request, so a retry under one request id is recognised as the same request. A row that
    already ends in that cell is left as it is, and continuation lines are left alone.
    """
    cell = f"req={request_id}"
    lines: list[str] = []
    for line in rows.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        ending = line[len(body) :]
        if _ROW_START.match(body) and not body.endswith(f" | {cell}"):
            body = f"{body} | {cell}"
        lines.append(body + ending)
    return "".join(lines)


def _refusal_text(done: subprocess.CompletedProcess[str]) -> str:
    """Why the append command refused: the receipt it printed on stdout, else the stderr tail.

    The command prints its refusal as one JSON receipt on stdout and writes only diagnostics
    to stderr, so the reason is the receipt's message and never the stderr tail alone.
    """
    last = done.stdout.strip().splitlines()[-1:] or [""]
    try:
        receipt = json.loads(last[0])
    except json.JSONDecodeError:
        receipt = None
    if isinstance(receipt, dict) and receipt.get("message"):
        return str(receipt["message"])[:400]
    return done.stderr[-400:] or "refused-without-reason"


class CommandLedgerAppender:
    """Appends through the configured bus append command: rows on stdin, one JSON receipt out."""

    def __init__(
        self, argv: Sequence[str], lane: str, host: str, timeout_seconds: int
    ) -> None:
        self._argv = list(argv)
        self._lane = lane
        self._host = host
        self._timeout = timeout_seconds

    def append(self, rows: str, request_id: str) -> ModelAppendOutcome:
        argv = [
            *self._argv,
            "--lane",
            self._lane,
            "--host",
            self._host,
            "--request-id",
            request_id,
            "--rows-file",
            "-",
        ]
        try:
            done = subprocess.run(
                argv,
                input=stamp_request_cell(rows, request_id),
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ModelAppendOutcome(
                status="error", message=f"append-command-failed:{type(exc).__name__}"
            )
        if done.returncode == 75:
            return ModelAppendOutcome(status="pending", message="no receipt in time")
        if done.returncode == 65:
            return ModelAppendOutcome(status="refused", message=_refusal_text(done))
        if done.returncode != 0:
            return ModelAppendOutcome(
                status="error", message=f"append-command-exit-{done.returncode}"
            )
        last = done.stdout.strip().splitlines()[-1:] or [""]
        try:
            receipt = json.loads(last[0])
        except json.JSONDecodeError:
            return ModelAppendOutcome(status="error", message="receipt-not-json")
        status = receipt.get("status") if isinstance(receipt, dict) else None
        if status in ("accepted", "duplicate", "already-present"):
            return ModelAppendOutcome(
                status="duplicate" if status != "accepted" else "accepted",
                message=str(receipt.get("message", ""))[:300],
            )
        return ModelAppendOutcome(
            status="pending" if status == "pending" else "error",
            message=f"receipt-status-{status}",
        )


def _required_env(name: str, environ: Mapping[str, str]) -> str:
    value = environ.get(name, "").strip()
    if not value:
        raise LedgerRouteError(
            f"{name} is not set; the overlay names it as the ledger route"
        )
    return value


@singledispatch
def build_row_source(
    route: ModelLedgerReadRoute, environ: Mapping[str, str] | None = None
) -> ProtocolLedgerRowSource:
    """Build the transport selected and validated by the overlay's tagged schema."""
    raise LedgerRouteError(
        f"unsupported ledger read configuration: {type(route).__name__}"
    )


@build_row_source.register
def _file_row_source(
    route: ModelFileLedgerReadRoute, environ: Mapping[str, str] | None = None
) -> ProtocolLedgerRowSource:
    env = os.environ if environ is None else environ
    live = Path(_required_env(route.path_env, env))
    archive = live.parent / route.archive_subdir if route.archive_subdir else None
    return FileLedgerRowSource(live, archive)


@build_row_source.register
def _command_row_source(
    route: ModelCommandLedgerReadRoute, environ: Mapping[str, str] | None = None
) -> ProtocolLedgerRowSource:
    env = os.environ if environ is None else environ
    return CommandLedgerRowSource(
        shlex.split(_required_env(route.command_env, env)),
        route.timeout_seconds,
        route.row_limit,
    )


def build_appender(
    route: ModelLedgerAppendRoute, lane: str, environ: Mapping[str, str] | None = None
) -> ProtocolReadingAppender:
    env = os.environ if environ is None else environ
    return CommandLedgerAppender(
        shlex.split(_required_env(route.command_env, env)),
        lane,
        _required_env(route.host_env, env),
        route.timeout_seconds,
    )
