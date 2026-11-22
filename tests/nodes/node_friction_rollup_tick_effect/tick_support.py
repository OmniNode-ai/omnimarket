# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared helpers of the friction rollup tick tests: the recorded fixtures, a runner and the parity comparison."""

from __future__ import annotations

import json
import re
import shlex
import shutil
import sys
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from omnimarket.models.model_point_query import ModelPointQuery
from omnimarket.models.model_work_ledger_reader import ModelPointAnswer
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.friction_rollup_tick_io import (
    load_settings,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.handlers.handler_friction_rollup_tick import (
    DEFAULT_OVERLAY,
    HandlerFrictionRollupTick,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.models.model_friction_rollup_tick import (
    ModelFrictionRollupTickRequest,
    ModelFrictionRollupTickResult,
    ModelFrictionRollupTickSettings,
    ModelTickLedgerRead,
)
from omnimarket.nodes.node_friction_rollup_tick_effect.protocols.protocol_bus_ledger_appender import (
    InMemoryBusLedgerAppender,
    ProtocolBusLedgerAppender,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "tests/fixtures/friction_rollup_tick"
PARITY = json.loads(
    (ROOT / "tests/fixtures/friction_rollup_tick_parity.json").read_text(
        encoding="utf-8"
    )
)
STATE_MARK = "<STATE>"
#: Where the timer recorded its reports. The node names the effective report directory in the STATUS row, so a test that
#: passes its own state directory maps that directory back to this recorded form before it compares with the timer.
RECORDED_REPORT_DIR = ".onex_state/friction-rollup"
#: Totals keys the compute node's report carries and the timer's rollup (the unit's main branch) did not: they were
#: added by OMN-22563 (class rules). On the recorded rows no rule matches, so each is 0; the tests assert that.
ADDITIVE_TOTALS_KEYS = ("rows_classified_by_rule",)


_ROW_START = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) \| ([A-Z][A-Z-]*) \|")


class FixtureLedgerRead:
    """The bus read port over recorded ledger files: it answers a typed rows query the way the query node does.

    Rows are the files' lines that start with a stamp and a row type; the answer is paged with ``page_size`` and an offset
    cursor, filtered by the window, the kinds and the term. ``requests`` keeps every query.
    """

    def __init__(self, files: list[Path]) -> None:
        self.requests: list[ModelPointQuery] = []
        self._rows: list[tuple[datetime, str, str]] = []
        for path in files:
            for line in path.read_text(encoding="utf-8").splitlines():
                match = _ROW_START.match(line)
                if match is not None:
                    stamp = datetime.fromisoformat(
                        match.group(1).replace("Z", "+00:00")
                    )
                    self._rows.append((stamp, match.group(2), line))

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        self.requests.append(query)
        assert query.query == "rows"
        assert query.page_size is not None
        term = (query.term or "").lower()
        matched = [
            line
            for stamp, kind, line in self._rows
            if (query.since is None or stamp >= query.since)
            and (query.until is None or stamp < query.until)
            and (not query.kinds or kind in query.kinds)
            and term in line.lower()
        ]
        start = int(query.cursor or 0)
        page = matched[start : start + query.page_size]
        more = start + query.page_size < len(matched)
        return ModelPointAnswer(
            lines=tuple(page),
            matched=len(matched),
            next_cursor=str(start + query.page_size) if more else None,
        )


def ledger_files(ledger: Path) -> list[Path]:
    """The recorded ledger and its archive files, the rows the timer read from the file system."""
    return [ledger, *sorted((ledger.parent / "archive").glob("*-split.md"))]


def row_kinds(files: list[Path]) -> tuple[str, ...]:
    kinds: set[str] = set()
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            match = _ROW_START.match(line)
            if match is not None:
                kinds.add(match.group(2))
    return tuple(sorted(kinds))


def settings_reading_every_kind(
    files: list[Path], **changes: Any
) -> ModelFrictionRollupTickSettings:
    """The overlay's settings with its reads replaced by one read of every row kind the files hold (the timer's whole-file read)."""
    return load_settings(DEFAULT_OVERLAY).model_copy(
        update={
            "ledger_reads": (
                ModelTickLedgerRead(kinds=row_kinds(files), why="every recorded row"),
            ),
            **changes,
        }
    )


def tick_handler(
    ledger: Path,
    appender: ProtocolBusLedgerAppender | None = None,
    *,
    every_kind: bool = True,
    environ: Mapping[str, str] | None = None,
    source: ProtocolLedgerPointRead | None = None,
    **changes: Any,
) -> HandlerFrictionRollupTick:
    """A tick over the recorded ledger served as a bus read; ``every_kind`` reads all kinds (parity with the timer)."""
    files = ledger_files(ledger)
    settings = (
        settings_reading_every_kind(files, **changes)
        if every_kind
        else load_settings(DEFAULT_OVERLAY).model_copy(update=changes)
    )
    return HandlerFrictionRollupTick(
        appender=appender if appender is not None else InMemoryBusLedgerAppender(),
        settings=settings,
        environ=environ,
        source=source if source is not None else FixtureLedgerRead(files),
    )


def timer_row(row: str, state_dir: Path) -> str:
    """The STATUS row with the effective report directory written as the timer recorded it."""
    return row.replace(state_dir.as_posix(), RECORDED_REPORT_DIR)


def without_additive(json_text: str) -> str:
    """The report JSON with the additive totals keys removed (asserting each is 0) and the sources block dropped (see
    ``recorded_json``), formatted as the tick writes it."""
    document = json.loads(json_text)
    for window in document["windows"].values():
        for key in ADDITIVE_TOTALS_KEYS:
            removed = window["totals"].pop(key)
            assert removed == 0
    return recorded_json(json.dumps(document, indent=2, ensure_ascii=False) + "\n")


def _without_provenance(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_provenance(item)
            for key, item in value.items()
            if key not in ("source", "line", "sources")
        }
    if isinstance(value, list):
        return [_without_provenance(item) for item in value]
    return value


def recorded_json(json_text: str) -> str:
    """The report JSON without where each row was read from.

    The timer named the ledger file, the archive file and the line of each row it read (``sources``, ``source`` and ``line``);
    the tick reads the window over the bus, so its rows have no file or line. Every other value of the report is compared.
    """
    return (
        json.dumps(
            _without_provenance(json.loads(json_text)), indent=2, ensure_ascii=False
        )
        + "\n"
    )


def recorded_markdown(markdown_text: str) -> str:
    """The report markdown without the ``Sources:`` line and the ``(file:line)`` of each row (see ``recorded_json``)."""
    kept = [
        line for line in markdown_text.split("\n") if not line.startswith("Sources: ")
    ]
    return re.sub(r" \([^()\s]+:\d+\)", "", "\n".join(kept))


def install_ledger(tmp_path: Path, *, archive: bool = True) -> Path:
    """Copy the recorded ledger (and its archive) under tmp_path and return the ledger file."""
    ledger_dir = tmp_path / "ledger"
    ledger_dir.mkdir(parents=True, exist_ok=True)
    ledger = ledger_dir / "ledger.md"
    shutil.copy(FIXTURES / "ledger_real_rows.md", ledger)
    if archive:
        shutil.copytree(
            FIXTURES / "archive", ledger_dir / "archive", dirs_exist_ok=True
        )
    return ledger


def request_for(
    tmp_path: Path, ledger: Path, now: str | None, *, dry_run: bool = False
) -> ModelFrictionRollupTickRequest:
    """The request of a tick; ``ledger`` is the recorded file the test's read port serves, never a path the request names."""
    return ModelFrictionRollupTickRequest(
        registry_root=str(tmp_path / "registry"),
        now=now,
        dry_run=dry_run,
        state_dir=str(tmp_path / "state"),
    )


def run_case(
    tmp_path: Path, case: dict[str, Any]
) -> list[tuple[dict[str, Any], ModelFrictionRollupTickResult]]:
    """Run every recorded tick of a case through the node, in order, against one state directory."""
    ledger = install_ledger(tmp_path, archive=bool(case["archive"]))
    handler = tick_handler(ledger)
    return [
        (
            tick,
            handler.handle(
                request_for(
                    tmp_path, ledger, tick["now"], dry_run=bool(tick["dry_run"])
                )
            ),
        )
        for tick in case["ticks"]
    ]


def observed(result: ModelFrictionRollupTickResult, state_dir: Path) -> dict[str, Any]:
    """The node's output in the shape the timer's recorded tick has."""
    stdout = result.log_line.replace(str(state_dir), STATE_MARK) + "\n"
    if result.state == "DRY-RUN":
        stdout = timer_row(result.status_row, state_dir) + "\n" + stdout
    return {
        "exit_code": result.exit_code,
        "stdout": stdout,
        "row": timer_row(result.status_row, state_dir),
        "json_text": without_additive(result.report_json_text),
        "markdown_text": recorded_markdown(result.report_markdown_text),
    }


def tick_diff(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    """Every field of the recorded tick the node's output does not equal, with its first differing line."""
    differences: list[str] = []
    for field in ("exit_code", "stdout", "row", "json_text", "markdown_text"):
        if field not in expected:
            continue
        want, got = expected[field], actual[field]
        if field == "json_text":
            want = recorded_json(want)
        if field == "markdown_text":
            want = recorded_markdown(want)
        if want == got:
            continue
        if isinstance(want, str) and isinstance(got, str):
            want_lines, got_lines = want.splitlines(), got.splitlines()
            at = next(
                (
                    i
                    for i, pair in enumerate(zip(want_lines, got_lines, strict=False))
                    if pair[0] != pair[1]
                ),
                min(len(want_lines), len(got_lines)),
            )
            differences.append(
                f"{field}: line {at + 1}: timer={want_lines[at : at + 1]!r} node={got_lines[at : at + 1]!r}"
            )
        else:
            differences.append(f"{field}: timer={want!r} node={got!r}")
    return differences


# --- the real command routes: a bus read command and a bus append command that run as separate processes --------------

READ_STUB = """\
import json, os, re, sys
from datetime import datetime
request = json.loads(sys.stdin.read())
if os.environ.get("STUB_READ_FAIL"):
    sys.stderr.write("stub read refused")
    sys.exit(int(os.environ["STUB_READ_FAIL"]))
row_start = re.compile(r"^(\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\dZ) \\| ([A-Z][A-Z-]*) \\|")
def stamp(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))
rows = []
for name in os.environ["STUB_LEDGER_FILES"].split(os.pathsep):
    for line in open(name, encoding="utf-8").read().splitlines():
        match = row_start.match(line)
        if match:
            rows.append((stamp(match.group(1)), match.group(2), line))
since = stamp(request["since"]) if request.get("since") else None
until = stamp(request["until"]) if request.get("until") else None
kinds = request.get("kinds") or []
term = (request.get("term") or "").lower()
matched = [
    line for ts, kind, line in rows
    if (since is None or ts >= since) and (until is None or ts < until)
    and (not kinds or kind in kinds) and term in line.lower()
]
start = int(request.get("cursor") or 0)
size = int(request["page_size"])
page = matched[start:start + size]
answer = {"correlation_id": request["correlation_id"], "status": "ok", "lines": page, "matched": len(matched)}
if start + size < len(matched):
    answer["next_cursor"] = str(start + size)
if os.environ.get("STUB_READ_LOG"):
    open(os.environ["STUB_READ_LOG"], "a").write(json.dumps({k: request.get(k) for k in ("query", "kinds", "term", "page_size", "cursor")}) + "\\n")
print(json.dumps(answer))
"""

APPEND_STUB = """\
import json, os, sys
args = sys.argv[1:]
rows = sys.stdin.read()
open(os.environ["STUB_APPEND_LOG"], "a").write(json.dumps({"args": args, "rows": rows}) + "\\n")
code = int(os.environ.get("STUB_APPEND_EXIT", "0"))
if code == 0:
    print(json.dumps({"status": "accepted"}))
elif code == 65:
    print(json.dumps({"status": "refused", "message": "refused-by-stub"}))
else:
    sys.stderr.write("refused-by-stub")
sys.exit(code)
"""


def write_bus_stubs(directory: Path) -> tuple[str, str]:
    """The two commands as the unit's environment names them: ``(read command, append command)``.

    The read stub serves the files ``STUB_LEDGER_FILES`` names, paged with the request's ``page_size`` and cursor; the append
    stub appends the request's argv and rows to ``STUB_APPEND_LOG`` and exits with ``STUB_APPEND_EXIT``.
    """
    directory.mkdir(parents=True, exist_ok=True)
    read = directory / "bus_read_stub.py"
    append = directory / "bus_append_stub.py"
    read.write_text(READ_STUB, encoding="utf-8")
    append.write_text(APPEND_STUB, encoding="utf-8")
    return (
        shlex.join([sys.executable, str(read)]),
        shlex.join([sys.executable, str(append)]),
    )


def every_kind_overlay(directory: Path, files: list[Path]) -> Path:
    """A copy of the tick's overlay whose reads take every row kind of ``files`` (the timer's whole-file read)."""
    import yaml

    document = yaml.safe_load(DEFAULT_OVERLAY.read_text(encoding="utf-8"))
    document["ledger_reads"] = [
        {"kinds": list(row_kinds(files)), "why": "every recorded row"}
    ]
    path = directory / "overlay_every_kind.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def appended_rows(log: Path) -> list[str]:
    """The STATUS rows the append stub received, each without the ``req=`` cell the append command stamps."""
    rows: list[str] = []
    for line in log.read_text(encoding="utf-8").splitlines():
        text = json.loads(line)["rows"].rstrip("\n")
        rows.append(re.sub(r" \| req=[^|]+$", "", text))
    return rows
