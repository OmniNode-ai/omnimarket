"""Queue exceptions and mechanize their second occurrence (OMN-17427).

Operator 2026-10-18 ~22:30Z: "need to make the process as mechanical as possible, where you only have to rule on the
exceptions instead of the rule." (5) Every detector and actor resolves its case in code or emits one typed exception
row (class, evidence, what it tried, proposed fix); the operator rules only on rows marked decision. (6) A second
exception of the same class makes the queue dispatch a lane to mechanize that class.

The queue is a JSON-lines file in the state directory: one row per case, and a ``resolves`` line that closes a row.
A case is its class and key: the same open case reported again is one row, so a detector that re-reads it every
tick never counts it twice; a case that recurs after it was resolved is a new occurrence. On a class's second
occurrence the emit writes a mechanize brief (every row of the class, verbatim) and starts it detached through the
remote-lane runner, once per class; a refused start is recorded unsent and the class's next emit retries it.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_lane_model_policy_compute.handlers.handler_lane_model_policy import (
    require_lane_model,
)

from .models import (
    ModelExceptionCliRequest,
    ModelExceptionCliResult,
    ModelExceptionEmitRequest,
    ModelExceptionEmitResult,
    ModelExceptionQueueRead,
    ModelExceptionQueueReadRequest,
    ModelExceptionQueueRequest,
    ModelExceptionQueueResult,
    ModelExceptionResolveRequest,
    ModelExceptionResolveResult,
    ModelExceptionRow,
)

QUEUE_FILE = "exception-queue.jsonl"
MECHANIZE_FILE = "exception-queue.mechanize.json"
TICKET = "OMN-17427"
AUDIENCES = ("operator", "orchestrator")
# The remote-lane runner is the file ONEX_EXCEPTION_RUNNER names; with none set the queue starts no dispatch.
RUNNER: Path | None = None


def state_dir() -> Path:
    root = os.environ.get("ONEX_STATE_DIR") or str(
        Path(os.environ.get("ONEX_REGISTRY_ROOT") or ".") / ".onex_state"
    )
    return Path(root) / "exception-queue"


@contextmanager
def _locked(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    with (state_dir / "exception-queue.lock").open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _read_queue(state_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    opened: list[dict[str, Any]] = []
    path = state_dir / QUEUE_FILE
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "resolves" in row:
                opened = [r for r in opened if r["id"] != row["resolves"]]
            else:
                rows.append(row)
                opened.append(row)
    return rows, opened


def _append(state_dir: Path, row: dict[str, Any]) -> None:
    with (state_dir / QUEUE_FILE).open("a", encoding="utf-8") as queue:
        queue.write(json.dumps(row) + "\n")


def _mechanize_records(state_dir: Path) -> dict[str, Any]:
    path = state_dir / MECHANIZE_FILE
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _single_line(value: object) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return " ".join(str(value).replace("|", " ").splitlines())


def _brief(
    request: ModelExceptionEmitRequest,
    lane: str,
    rows: list[dict[str, Any]],
    model: str,
) -> Path:
    cls = request.row.cls
    lines = [
        f"# Lane {lane}: mechanize exception class {cls} ({TICKET})",
        f"ROUTE: model={model} effort=high",
        f"Parent: {request.parent or 'orchestrator'}. Ticket: {TICKET}. "
        f"Repo: {next((r['repo'] for r in reversed(rows) if r.get('repo')), '') or 'the class names it'}.",
        'Authority: Operator 2026-10-18 ~22:30Z: "need to make the process as mechanical as possible, '
        'where you only have to rule on the exceptions instead of the rule."',
        "(6) SECOND OCCURRENCE BECOMES CODE",
        "",
    ]
    for row in rows:
        for field in ("id", "key", "source", "evidence", "tried", "proposed_fix"):
            lines.append(f"{field}: {row[field]}")
        lines.append("")
    lines.extend(
        [
            "Build a detector that recognises this class, an actor that resolves it in code, and a scenario in the",
            "friction replay suite (lane friction-replay-suite) that replays these rows.",
            "The actor emits to the exception queue (queue_status.py --exception emit) only a case it still cannot resolve.",
            "A decision row is never resolved by the lane.",
            "Read PRs only with pr_state_local.py; never merge, arm, rerun or poll CI.",
            "/omni:pr-handoff the PR; report once at terminal.",
        ]
    )
    path = request.state_dir / "briefs" / f"{lane}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _mechanize(request: ModelExceptionEmitRequest, rows: list[dict[str, Any]]) -> str:
    records = _mechanize_records(request.state_dir)
    previous = records.get(request.row.cls, {})
    if previous.get("sent"):
        return f"already:{previous['lane']}"
    lane = "mechanize-" + request.row.cls[:48].rstrip("-")
    model = require_lane_model(lane_kind="mechanize")
    brief = _brief(request, lane, rows, model)
    repo = next((row["repo"] for row in reversed(rows) if row.get("repo")), "")
    sent, detail = False, "no-runner"
    if request.runner is not None:
        command = [
            sys.executable,
            "-B",
            str(request.runner),
            "run",
            "--detach",
            "--placement-wait-s",
            "60",
            "--brief",
            str(brief),
            "--lane",
            lane,
            "--ticket",
            TICKET,
            "--model",
            model,
            "--effort",
            "high",
            "--priority-class",
            "process-fix",
        ]
        if repo:
            command.extend(["--repo", repo])
        if request.parent:
            command.extend(["--parent", request.parent])
        try:
            done = subprocess.run(
                command,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=90,
                check=False,
            )
            sent = done.returncode in (0, 3)
            detail = (
                _single_line(done.stdout + "\n" + done.stderr).strip()[-200:]
                or f"exit {done.returncode}"
            )
        except subprocess.TimeoutExpired as exc:
            output = (
                _single_line(exc.stdout or "") + " " + _single_line(exc.stderr or "")
            )
            sent = "DETACHED lane=" in output
            detail = output.strip()[-200:] or "runner timeout"
        except OSError as exc:
            detail = _single_line(exc)[-200:]
    records[request.row.cls] = {
        "lane": lane,
        "at": request.now,
        "sent": sent,
        "detail": detail,
    }
    (request.state_dir / MECHANIZE_FILE).write_text(
        json.dumps(records) + "\n", encoding="utf-8"
    )
    return f"sent:{lane}" if sent else f"unsent:{detail}"


class HandlerExceptionEmit:
    def handle(self, request: ModelExceptionEmitRequest) -> ModelExceptionEmitResult:
        row = request.row
        invalid = [
            field
            for field in ("key", "source", "evidence", "tried", "proposed_fix")
            if not getattr(row, field).strip()
        ]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,59}", row.cls):
            invalid.append("cls (must be a kebab slug)")
        if invalid:
            return ModelExceptionEmitResult(
                "refused", "", 0, "", "Invalid fields: " + ", ".join(invalid)
            )
        with _locked(request.state_dir):
            rows, opened = _read_queue(request.state_dir)
            same_class = [r for r in rows if r["cls"] == row.cls]
            existing = next(
                (r for r in opened if r["cls"] == row.cls and r["key"] == row.key), None
            )
            if existing is not None:
                status, row_id = "repeat", existing["id"]
            else:
                row_id = f"{request.now}-{row.cls}-{hashlib.sha1(row.key.encode('utf-8')).hexdigest()[:8]}"
                stored = {"id": row_id, "at": request.now, **asdict(row)}
                _append(request.state_dir, stored)
                same_class.append(stored)
                status = "appended"
            occurrence = len(same_class)
            mechanize = _mechanize(request, same_class) if occurrence >= 2 else ""
            return ModelExceptionEmitResult(status, row_id, occurrence, mechanize, "")


class HandlerExceptionQueue:
    def handle(self, request: ModelExceptionQueueRequest) -> ModelExceptionQueueResult:
        if request.audience not in ("operator", "orchestrator"):
            raise ValueError(f"Unknown exception queue audience: {request.audience}")
        with _locked(request.state_dir):
            rows, opened = _read_queue(request.state_dir)
            records = _mechanize_records(request.state_dir)
        counts = Counter(row["cls"] for row in rows)
        operator = request.audience == "operator"
        selected = [row for row in opened if bool(row.get("decision")) == operator]
        lines = []
        for row in selected:
            cells = [
                f"DECISION {_single_line(row['id'])}",
                f"class={_single_line(row['cls'])}",
            ]
            if not operator:
                record = records.get(row["cls"], {})
                mechanize = (
                    f"sent:{record['lane']}"
                    if record.get("sent")
                    else "unsent"
                    if record
                    else "none"
                )
                cells[0] = f"EXCEPTION {_single_line(row['id'])}"
                cells.extend(
                    [
                        f"occurrence={counts[row['cls']]}",
                        f"mechanize={_single_line(mechanize)}",
                    ]
                )
            cells.extend(
                f"{field}={_single_line(row[field])}"
                for field in ("key", "source", "evidence", "tried", "proposed_fix")
            )
            lines.append(" | ".join(cells))
        if not operator:
            for cls, count in counts.items():
                record = records.get(cls, {})
                if count >= 2 and not record.get("sent"):
                    detail = record.get("detail", "no mechanize attempt recorded")
                    lines.append(
                        f"MISSING mechanize:{_single_line(cls)} | occurrence={count} | {_single_line(detail)}"
                    )
        return ModelExceptionQueueResult(selected, lines)


class HandlerExceptionResolve:
    def handle(
        self, request: ModelExceptionResolveRequest
    ) -> ModelExceptionResolveResult:
        with _locked(request.state_dir):
            _, opened = _read_queue(request.state_dir)
            if not any(row["id"] == request.id for row in opened):
                return ModelExceptionResolveResult(
                    "unknown-id", "No open exception with this id"
                )
            _append(
                request.state_dir,
                {
                    "resolves": request.id,
                    "at": request.now,
                    "by": request.by,
                    "how": request.how,
                },
            )
            return ModelExceptionResolveResult("resolved", "")


# ---- queue_status.py's surfaces: the --exception command and the block's read (OMN-17427) --------------------------

CLI = "queue_status.py --exception"


class HandlerExceptionQueueRead:
    """One ``exceptions`` line and the verdict's reasons and actions: decision rows for the operator, open rows for the
    orchestrator to drain, and classes seen twice with no mechanize lane sent. An unreadable queue is a line, never a
    crash of the read."""

    def handle(
        self, request: ModelExceptionQueueReadRequest
    ) -> ModelExceptionQueueRead:
        where = request.state_dir or state_dir()
        try:
            op, orch = (
                HandlerExceptionQueue().handle(ModelExceptionQueueRequest(where, a))
                for a in AUDIENCES
            )
        except (OSError, ValueError, KeyError) as exc:
            return ModelExceptionQueueRead(
                f"exceptions unreadable: {QUEUE_FILE}: {_single_line(exc)[-200:]}",
                [],
                [],
            )
        missing = [
            line.split(" | ", 1)[0].removeprefix("MISSING mechanize:")
            for line in orch.lines
            if line.startswith("MISSING mechanize:")
        ]
        reasons: list[str] = []
        actions: list[str] = []
        if op.rows:
            reasons.append(
                f"exception queue: {len(op.rows)} decision row(s) await an operator ruling"
            )
            actions.append(
                f"put each DECISION row of `{CLI} list --audience operator` to the operator as one question, "
                f"record the answer with /omni:operator-ruling, then `{CLI} resolve --id <id> --by <lane> "
                "--how <ruling>`"
            )
        if orch.rows:
            reasons.append(f"exception queue: {len(orch.rows)} open exception row(s)")
            actions.append(
                f"drain each EXCEPTION row of `{CLI} list`: apply its proposed fix or dispatch a lane for it, "
                f"then `{CLI} resolve --id <id> --by <lane> --how <what was done>`"
            )
        if missing:
            reasons.append(
                f"exception queue: class(es) seen twice with no mechanize lane: {' '.join(missing[:6])}"
            )
            actions.append(
                f"dispatch the mechanize brief under {where}/briefs/ through the remote-lane runner "
                "(the class's next emit retries it by itself)"
            )
        line = f"exceptions decision={len(op.rows)} open={len(orch.rows)} unmechanized={len(missing)}"
        return ModelExceptionQueueRead(line, reasons, actions)


class HandlerExceptionCli:
    """``queue_status.py --exception emit|list|resolve``: parse, run the handler, print, and return the exit code.
    The runner is ``ONEX_EXCEPTION_RUNNER`` when set; ``--no-dispatch`` starts none."""

    def handle(self, request: ModelExceptionCliRequest) -> ModelExceptionCliResult:
        ap = argparse.ArgumentParser(prog=CLI, allow_abbrev=False)
        ap.add_argument("verb", choices=("emit", "list", "resolve"))
        ap.add_argument("--state-dir", type=Path, default=None)
        ap.add_argument("--class", dest="cls", default="")
        for flag in (
            "--key",
            "--source",
            "--evidence",
            "--tried",
            "--proposed-fix",
            "--ticket",
            "--repo",
            "--id",
            "--by",
            "--how",
        ):
            ap.add_argument(flag, default="")
        ap.add_argument("--parent")
        ap.add_argument("--decision", action="store_true")
        ap.add_argument("--no-dispatch", action="store_true")
        ap.add_argument("--audience", choices=AUDIENCES, default="orchestrator")
        ap.add_argument("--now", default=None)
        try:
            args = ap.parse_args(request.argv)
        except SystemExit as exc:
            return ModelExceptionCliResult(int(exc.code or 0), "")
        where = args.state_dir or state_dir()
        now = args.now or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        if args.verb == "emit":
            env_runner = os.environ.get("ONEX_EXCEPTION_RUNNER")
            runner = (
                None
                if args.no_dispatch
                else (Path(env_runner) if env_runner else RUNNER)
            )
            row = ModelExceptionRow(
                args.cls,
                args.key,
                args.source,
                args.evidence,
                args.tried,
                args.proposed_fix,
                args.decision,
                args.ticket,
                args.repo,
            )
            result = HandlerExceptionEmit().handle(
                ModelExceptionEmitRequest(where, row, now, runner, args.parent)
            )
            detail = f" | {result.detail}" if result.detail else ""
            out = (
                f"EXCEPTION-QUEUE {result.status} id={result.id} class={args.cls} occurrence={result.occurrence} "
                f"mechanize={result.mechanize or 'none'}{detail}"
            )
            code = 3 if result.status == "refused" else 0
        elif args.verb == "list":
            listed = HandlerExceptionQueue().handle(
                ModelExceptionQueueRequest(where, args.audience)
            )
            label = "decision" if args.audience == "operator" else "open"
            out = "\n".join(
                [
                    f"EXCEPTION-QUEUE {args.audience} {label}={len(listed.rows)}",
                    *listed.lines,
                ]
            )
            code = 0
        else:
            done = HandlerExceptionResolve().handle(
                ModelExceptionResolveRequest(where, args.id, args.by, args.how, now)
            )
            out, code = (
                f"EXCEPTION-QUEUE {done.status} id={args.id}",
                0 if done.status == "resolved" else 3,
            )
        sys.stdout.write(out + "\n")
        return ModelExceptionCliResult(code, out)
