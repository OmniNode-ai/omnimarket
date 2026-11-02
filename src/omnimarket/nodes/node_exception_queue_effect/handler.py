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
from typing import TYPE_CHECKING, Any

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
    ModelOpenStopsRequest,
    ModelOpenStopsResult,
    ModelStopEventRequest,
    ModelStopEventResult,
)

if TYPE_CHECKING:
    from omnibase_core.models.contracts.subcontracts.model_fsm_subcontract import (
        ModelFSMSubcontract,
    )

    from omnimarket.models.stop_gate import ModelAndonStopConfig

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
            elif "stop_update" in row:
                for held in rows:
                    if held["id"] == row["stop_update"]:
                        held["stop"] = row["stop"]
            elif "stop_refused" in row:
                continue
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
    repo = next((r["repo"] for r in reversed(rows) if r.get("repo")), "")
    lines = [
        f"# Lane {lane}: mechanize exception class {cls} ({TICKET})",
        f"ROUTE: model={model} effort=high",
        f"Parent: {request.parent or 'orchestrator'}. Ticket: {TICKET}. "
        f"Repo: {repo or 'the class names it'}.",
        *([f"Opens PR in: {repo}"] if repo else []),
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


def _contract() -> tuple[ModelFSMSubcontract, ModelAndonStopConfig]:
    from omnimarket.handlers.rules_stop_gate import load_andon_stop_contract

    return load_andon_stop_contract()


def _resolve_contract(
    fsm: ModelFSMSubcontract | None, config: ModelAndonStopConfig | None
) -> tuple[ModelFSMSubcontract, ModelAndonStopConfig]:
    """The injected machine and config, else the packaged reducer contract's."""
    if fsm is not None and config is not None:
        return fsm, config
    loaded_fsm, loaded_config = _contract()
    return (fsm or loaded_fsm), (config or loaded_config)


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


class HandlerExceptionEmit:
    """Queue one row. A row whose class is in the stop registry of the reducer's contract also opens a stop, and
    only a caller of the contract's opening role may; any other class opens a failure record and no stop."""

    def __init__(
        self,
        *,
        fsm: ModelFSMSubcontract | None = None,
        config: ModelAndonStopConfig | None = None,
    ) -> None:
        self._fsm = fsm
        self._config = config

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
        fsm, config = _resolve_contract(self._fsm, self._config)
        stop_class = config.stop_class(row.cls)
        with _locked(request.state_dir):
            rows, opened = _read_queue(request.state_dir)
            same_class = [r for r in rows if r["cls"] == row.cls]
            existing = next(
                (r for r in opened if r["cls"] == row.cls and r["key"] == row.key), None
            )
            stop_note = "none:class_not_registered"
            if existing is not None:
                status, row_id = "repeat", existing["id"]
                if stop_class is not None:
                    stop_note = f"repeat:{row_id}"
            else:
                row_id = f"{request.now}-{row.cls}-{hashlib.sha1(row.key.encode('utf-8')).hexdigest()[:8]}"
                stored = {"id": row_id, "at": request.now, **asdict(row)}
                if stop_class is not None:
                    from omnimarket.models.stop_gate import (
                        OPEN_ROLE_KEY,
                        ModelStopRecord,
                        ModelStopScope,
                    )

                    expected = config.writer_roles[OPEN_ROLE_KEY].value
                    if request.role != expected:
                        reason = f"role_not_allowed:{request.role or 'none'}"
                        _append(
                            request.state_dir,
                            {
                                "stop_refused": row_id,
                                "at": request.now,
                                "event": OPEN_ROLE_KEY,
                                "role": request.role,
                                "reason": reason,
                            },
                        )
                        return ModelExceptionEmitResult(
                            "refused",
                            "",
                            0,
                            "",
                            f"A stop opens only from role {expected}: {reason}",
                        )
                    record = ModelStopRecord(
                        id=row_id,
                        cls=row.cls,
                        scope=ModelStopScope(
                            kind=stop_class.scope_key_kind, key=row.key
                        ),
                        state=fsm.initial_state,
                        stopped_at=_parse_time(request.now),
                        readback_command=row.readback_command,
                    )
                    stored["stop"] = record.model_dump(mode="json")
                    stop_note = f"opened:{row_id}"
                _append(request.state_dir, stored)
                same_class.append(stored)
                status = "appended"
            occurrence = len(same_class)
            mechanize = _mechanize(request, same_class) if occurrence >= 2 else ""
            return ModelExceptionEmitResult(
                status, row_id, occurrence, mechanize, "", stop_note
            )


class HandlerStopEvent:
    """Persist one writer's event to one stop: the role check and the transition are the reducer's rule, the
    append, the decision rows and the closing of a finished stop are the queue's. A refusal is recorded."""

    def __init__(
        self,
        *,
        fsm: ModelFSMSubcontract | None = None,
        config: ModelAndonStopConfig | None = None,
    ) -> None:
        self._fsm = fsm
        self._config = config

    def handle(self, request: ModelStopEventRequest) -> ModelStopEventResult:
        from omnimarket.handlers.rules_stop_gate import fold_stop_event
        from omnimarket.models.stop_gate import (
            EnumFoldStatus,
            ModelStopRecord,
        )

        fsm, config = _resolve_contract(self._fsm, self._config)
        event = request.event
        with _locked(request.state_dir):
            _, opened = _read_queue(request.state_dir)
            held = next(
                (r for r in opened if r["id"] == event.stop_id and r.get("stop")), None
            )

            def refuse(
                reason: str, stop: ModelStopRecord | None
            ) -> ModelStopEventResult:
                _append(
                    request.state_dir,
                    {
                        "stop_refused": event.stop_id,
                        "at": request.now,
                        "event": event.kind.value,
                        "role": event.role,
                        "reason": reason,
                    },
                )
                return ModelStopEventResult(
                    "refused",
                    reason,
                    stop,
                    0,
                    f"REFUSED stop:{event.stop_id} event={event.kind.value} reason={reason}",
                )

            if held is None:
                return refuse("unknown_stop", None)
            record = ModelStopRecord.model_validate(held["stop"])
            folded = fold_stop_event(record, event, fsm, config)
            if folded.status is EnumFoldStatus.REFUSED:
                return refuse(folded.reason, record)
            _append(
                request.state_dir,
                {
                    "stop_update": event.stop_id,
                    "at": request.now,
                    "event": event.kind.value,
                    "trigger": folded.trigger,
                    "stop": folded.stop.model_dump(mode="json"),
                },
            )
            decisions = folded.actions.count("emit_decision_row")
            for _ in range(decisions):
                key = (
                    f"{event.stop_id}:{folded.trigger}:{folded.stop.state}"
                    f":{folded.stop.lease_replacements_used}:{folded.stop.replacements_extended}"
                )
                _append(
                    request.state_dir,
                    {
                        "id": f"{request.now}-andon-stop-decision-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:8]}",
                        "at": request.now,
                        "cls": "andon-stop-decision",
                        "key": key,
                        "source": "node_andon_stop_reducer",
                        "evidence": folded.line,
                        "tried": f"folded {event.kind.value} from role {event.role} through the stop machine",
                        "proposed_fix": f"rule on stop {event.stop_id} ({record.cls} {record.scope.kind}:{record.scope.key})",
                        "decision": True,
                        "ticket": held.get("ticket", ""),
                        "repo": held.get("repo", ""),
                        "readback_command": "",
                    },
                )
            if folded.stop.state in fsm.terminal_states:
                _append(
                    request.state_dir,
                    {
                        "resolves": event.stop_id,
                        "at": request.now,
                        "by": "node_andon_stop_reducer",
                        "how": folded.stop.state,
                    },
                )
            return ModelStopEventResult(
                "applied", "", folded.stop, decisions, folded.line
            )


class HandlerOpenStops:
    """The stops not yet cleared or declared false, and the recorded refusals, for ``stop_gate`` and the read."""

    def handle(self, request: ModelOpenStopsRequest) -> ModelOpenStopsResult:
        from omnimarket.models.stop_gate import ModelStopRecord

        with _locked(request.state_dir):
            _, opened = _read_queue(request.state_dir)
            path = request.state_dir / QUEUE_FILE
            lines = (
                path.read_text(encoding="utf-8").splitlines() if path.exists() else []
            )
        refusals = tuple(
            json.loads(line)
            for line in lines
            if line.strip() and '"stop_refused"' in line
        )
        return ModelOpenStopsResult(
            tuple(
                ModelStopRecord.model_validate(r["stop"])
                for r in opened
                if r.get("stop")
            ),
            refusals,
        )


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

    def __init__(self, queue: HandlerExceptionQueue | None = None) -> None:
        if queue is None:
            from .container.container_exception_queue_effect import (
                create_handler_exception_queue,
            )

            queue = create_handler_exception_queue()
        self._queue = queue

    def handle(
        self, request: ModelExceptionQueueReadRequest
    ) -> ModelExceptionQueueRead:
        where = request.state_dir or state_dir()
        try:
            op, orch = (
                self._queue.handle(ModelExceptionQueueRequest(where, a))
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

    def __init__(
        self,
        emit: HandlerExceptionEmit | None = None,
        queue: HandlerExceptionQueue | None = None,
        resolve: HandlerExceptionResolve | None = None,
    ) -> None:
        from .container.container_exception_queue_effect import (
            create_handler_exception_emit,
            create_handler_exception_queue,
            create_handler_exception_resolve,
        )

        self._emit = emit if emit is not None else create_handler_exception_emit()
        self._queue = queue if queue is not None else create_handler_exception_queue()
        self._resolve = (
            resolve if resolve is not None else create_handler_exception_resolve()
        )

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
            "--role",
            "--readback-command",
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
                args.readback_command,
            )
            result = self._emit.handle(
                ModelExceptionEmitRequest(
                    where, row, now, runner, args.parent, args.role
                )
            )
            detail = f" | {result.detail}" if result.detail else ""
            out = (
                f"EXCEPTION-QUEUE {result.status} id={result.id} class={args.cls} occurrence={result.occurrence} "
                f"mechanize={result.mechanize or 'none'}{detail}"
            )
            code = 3 if result.status == "refused" else 0
        elif args.verb == "list":
            listed = self._queue.handle(
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
            done = self._resolve.handle(
                ModelExceptionResolveRequest(where, args.id, args.by, args.how, now)
            )
            out, code = (
                f"EXCEPTION-QUEUE {done.status} id={args.id}",
                0 if done.status == "resolved" else 3,
            )
        sys.stdout.write(out + "\n")
        return ModelExceptionCliResult(code, out)
