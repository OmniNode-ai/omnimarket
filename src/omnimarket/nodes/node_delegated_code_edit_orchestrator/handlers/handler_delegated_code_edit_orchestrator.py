# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerDelegatedCodeEditOrchestrator: the delegated code edit loop (OMN-20290).

The node path for the agentic code drafting crush does today (operator ruling
2026-10-01, RULING row 2026-10-01T11:28:57Z). Each model turn is one
``onex delegate`` run, so every model call is routed by the routing contract,
gated, receipted and projected like any other delegation. The loop around the
turns is this orchestrator:

    CLAIM -> [TURN -> APPLY(actions) -> (FINISH -> CHECKS)]* -> DIFF -> SCORE -> RECEIPT

* At most ``max_turns`` turns (the tool_use rubric's budget caps it at 40).
* Writes are confined: a write or edit whose path is absolute, climbs out with
  ``..``, touches ``.git``, or matches no writable glob is refused and the
  refusal is fed back. Nothing is written for it.
* ``run_check`` runs only a check the request declares, by name; an undeclared
  name is refused. There is no shell tool.
* ``finish`` runs every declared check. All passing ends the loop
  ``accepted``; otherwise the failures are fed back and the loop continues.
* Two failing check rounds in a row with the same check fingerprints over the
  same diff end the loop ``no_progress``.
* Two failed ``onex delegate`` runs in a row (no reply at all) end the loop
  ``delegate_failed``. An unusable reply is fed back like any other failure.
* At the turn cap the checks run once more; passing is ``accepted``, failing
  is ``budget_exhausted``.
* An existing loop receipt refuses a fresh run before any turn.

The receipt holds every turn's run id, actions and observations, the diff, the
check results and the tool_use rubric verdict. The result carries none of the
file content. Its ``resume`` block allows ``--resume`` after delegate_failed,
archiving the prior receipt as ``loop_receipt.<n>.json``; the turn cap spans
all resumed segments.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import posixpath
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Literal, cast

from omnimarket.nodes.node_delegated_code_edit_orchestrator.handlers.turn_protocol import (
    RESPONSE_CONTRACT,
    TOOL_SCHEMAS,
    build_turn_prompt,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.models.model_delegated_code_edit import (
    MAX_OBSERVATION_BYTES,
    MAX_VIEW_BYTES,
    MAX_VIEW_WINDOW_BYTES,
    MAX_WRITE_BYTES,
    VIEW_WINDOW_LINES,
    WRITING_TOOLS,
    EnumCodeEditStatus,
    EnumCodeEditTool,
    ModelCheckResult,
    ModelCodeEditAction,
    ModelCodeEditResult,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelObservation,
    ModelTurnReply,
    bound_error,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator.protocols.protocol_delegated_code_edit_ports import (
    ProtocolDelegatedCodeEditPorts,
    ResumeRefusedError,
    WorkspacePathError,
)

#: The turn prompt's character budget before it is shrunk to fit.
PROMPT_CHARS = 90_000
#: One argv word on Linux is bounded at 128 KiB; the prompt travels as one.
PROMPT_BYTES = 120_000


def normalise_path(path: str) -> str | None:
    """The worktree-relative form of ``path``, or None when it leaves the worktree."""
    if not path or path.startswith("/") or "\x00" in path:
        return None
    norm = posixpath.normpath(path)
    if norm == ".." or norm.startswith("../"):
        return None
    return norm


def glob_regex(glob: str) -> re.Pattern[str]:
    """A worktree glob as a regex: ``*`` and ``?`` stay inside one path segment,
    ``**`` crosses segments (``src/**/*.py`` matches ``src/a.py`` and ``src/b/c.py``)."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:[^/]+/)*")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def writable(request: ModelDelegatedCodeEditRequest, path: str) -> bool:
    """Whether ``path`` (already normalised) may be written."""
    if path == "." or path == ".git" or path.startswith(".git/"):
        return False
    return any(glob_regex(glob).match(path) for glob in request.writable_globs)


def _anchor_dir(path: str) -> str:
    """The directory a glob or path is rooted in, up to its first wildcard."""
    head = re.split(r"[*?\[]", path, maxsplit=1)[0]
    return posixpath.dirname(head) if head != path else posixpath.dirname(path)


def relevant_first(
    request: ModelDelegatedCodeEditRequest, paths: list[str]
) -> list[str]:
    """The file index with files near the writable and context paths first, so a
    large repository's index shows the model the files the task is about."""
    anchors = {
        _anchor_dir(p) for p in (*request.writable_globs, *request.context_paths)
    }
    anchors.discard("")

    def near(path: str) -> bool:
        return any(path.startswith(anchor + "/") for anchor in anchors)

    return [p for p in paths if near(p)] + [p for p in paths if not near(p)]


def _cap(text: str, limit: int = MAX_OBSERVATION_BYTES) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [{len(text) - limit} more characters cut]"


_LINE_PREFIX = re.compile(r"^ *\d+\| ?", re.M)


def view_window(text: str, path: str, offset: int) -> str:
    """One page of a file with line numbers, and how to see the rest."""
    lines = text.splitlines()
    total = len(lines)
    start = max(offset, 1)
    if total and start > total:
        return f"{path} has {total} lines; offset {start} is past the end"
    shown: list[str] = []
    size = 0
    end = start - 1
    for number in range(start, min(total, start - 1 + VIEW_WINDOW_LINES) + 1):
        row = f"{number:>5}| {lines[number - 1]}"
        if size + len(row) + 1 > MAX_VIEW_WINDOW_BYTES:
            break
        shown.append(row)
        size += len(row) + 1
        end = number
    head = f"[{path} lines {start}-{end} of {total}]"
    tail = f"\n[more: view {path} with offset={end + 1}]" if end < total else ""
    return head + "\n" + "\n".join(shown) + tail


def _edit_hint(current: str, old: str) -> str:
    """Where the first line of a missing old_string does occur, if anywhere."""
    first = next((line.strip() for line in old.splitlines() if line.strip()), "")
    if not first:
        return ""
    hits = [str(n) for n, line in enumerate(current.splitlines(), 1) if first in line]
    if not hits:
        return " Its first line occurs nowhere in the file; view the file again."
    return (
        f" Its first line occurs at line(s) {', '.join(hits[:5])}; view from there "
        "and copy old_string exactly, without the line-number prefix."
    )


@dataclass
class _Call:
    """One applied action, as the transcript records it."""

    call_id: str
    tool: str
    arguments: dict[str, object]
    ok: bool
    output: str


@dataclass
class _State:
    replies: list[ModelTurnReply] = field(default_factory=list)
    calls: list[_Call] = field(default_factory=list)
    history: list[str] = field(default_factory=list)
    turns: list[dict[str, object]] = field(default_factory=list)
    refusals: int = 0
    last_checks: tuple[ModelCheckResult, ...] = ()
    last_failure_key: str = ""
    summary: str = ""
    good_turn: int = 0
    good_history_len: int = 0
    superseded: list[dict[str, object]] = field(default_factory=list)
    resumes: int = 0

    def record_good(self, turn: int) -> None:
        """Snapshot the history boundary after a received reply was processed."""
        self.good_turn = turn
        self.good_history_len = len(self.history)


class _TerminalError(Exception):
    def __init__(self, status: EnumCodeEditStatus, detail: str = "") -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class HandlerDelegatedCodeEditOrchestrator:
    """ORCHESTRATOR: sequences the loop's turns through injected ports."""

    def __init__(self, ports: ProtocolDelegatedCodeEditPorts | None = None) -> None:
        # Optional so the handler constructs at boot from defaults alone; a run
        # without bound ports is refused rather than half-wired.
        self._bound = ports

    @property
    def _ports(self) -> ProtocolDelegatedCodeEditPorts:
        if self._bound is None:
            raise RuntimeError(
                "no loop ports are bound; `onex code-edit run` binds onex "
                "delegate, the worktree and the declared checks"
            )
        return self._bound

    @property
    def handler_type(self) -> Literal["NODE_HANDLER"]:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> Literal["ORCHESTRATOR"]:
        return "ORCHESTRATOR"

    async def handle(
        self, request: ModelDelegatedCodeEditRequest
    ) -> ModelCodeEditResult:
        return await asyncio.to_thread(self.run, request)

    # -- the sequence ---------------------------------------------------------

    def run(
        self, request: ModelDelegatedCodeEditRequest, *, resume: bool = False
    ) -> ModelCodeEditResult:
        started = time.monotonic()
        loop_run_id = request.correlation_id
        prior = self._resume_receipt(request) if resume else None
        self._ports.claim_loop_receipt(loop_run_id, resume=resume)
        state = self._seed(prior) if prior is not None else _State()
        status = EnumCodeEditStatus.INFRA_ERROR
        detail = ""
        manifest: tuple[tuple[str, int], ...] = ()
        try:
            manifest = self._ports.workspace_files(request)
            context = self._context(request)
            status = self._turns(request, manifest, context, state, state.good_turn + 1)
        except _TerminalError as terminal:
            status, detail = terminal.status, terminal.detail
        except WorkspacePathError as exc:
            status, detail = EnumCodeEditStatus.INFRA_ERROR, f"workspace: {exc}"
        except Exception as exc:
            status = EnumCodeEditStatus.INFRA_ERROR
            detail = f"unexpected {type(exc).__name__}: {exc}"

        try:
            diff = self._ports.diff(request)
        except Exception as exc:
            diff = ""
            if not detail:
                detail = f"diff failed: {type(exc).__name__}: {exc}"
        changed = tuple(
            sorted(
                {
                    line[len("+++ b/") :]
                    for line in diff.splitlines()
                    if line.startswith("+++ b/")
                }
            )
        )
        transcript = self._transcript(request, manifest, state, started)
        try:
            verdict = self._ports.score(request, transcript)
        except Exception as exc:
            verdict = {"error": f"{type(exc).__name__}: {exc}"}
        outcome = verdict.get("verdict", {})
        rubric_outcome = (
            str(outcome.get("outcome", "")) if isinstance(outcome, dict) else ""
        )
        resume_block = self._resume_block(request, status, diff, state)
        result = ModelCodeEditResult(
            loop_run_id=loop_run_id,
            status=status,
            turns=len(state.replies),
            delegate_run_ids=tuple(r.run_id for r in state.replies if r.run_id),
            changed_paths=changed,
            diff_sha256=hashlib.sha256(diff.encode()).hexdigest() if diff else "",
            resumable=bool(resume_block["resumable"]),
            checks=state.last_checks,
            refusals=state.refusals,
            rubric_outcome=rubric_outcome,
            local_tokens_in=sum(r.tokens_in for r in state.replies),
            local_tokens_out=sum(r.tokens_out for r in state.replies),
            wall_ms=int((time.monotonic() - started) * 1000),
            summary=state.summary[:1000],
            detail=bound_error(detail, 300),
        )
        self._ports.write_loop_receipt(
            loop_run_id,
            {
                "schema": "delegated-code-edit-loop-receipt.v1",
                "loop_run_id": loop_run_id,
                "correlation_id": request.correlation_id,
                "request": request.model_dump(mode="json"),
                "delegate_run_ids": list(result.delegate_run_ids),
                "turns": state.turns,
                "resume": resume_block,
                "superseded_turns": state.superseded,
                "resumes": state.resumes,
                "diff": diff,
                "checks": [c.model_dump(mode="json") for c in state.last_checks],
                "rubric_verdict": verdict,
                "result": result.model_dump(mode="json"),
                "error": {
                    "status": status.value,
                    "detail": bound_error(detail),
                    "turns": len(state.replies),
                }
                if status != EnumCodeEditStatus.ACCEPTED
                else None,
            },
        )
        return result

    def _resume_receipt(
        self, request: ModelDelegatedCodeEditRequest
    ) -> dict[str, object]:
        """Refuse unsafe resumes before acquiring or archiving a claim."""
        receipt = self._ports.load_loop_receipt(request.correlation_id)
        if receipt is None:
            raise ResumeRefusedError("there is no readable receipt to resume")
        block = receipt.get("resume")
        if not isinstance(block, dict):
            raise ResumeRefusedError(
                "the receipt carries no resume block; it predates resume support"
            )
        if not block.get("resumable"):
            raise ResumeRefusedError(
                str(block.get("reason", "the loop is not resumable"))
            )
        if receipt.get("request") != request.model_dump(mode="json"):
            raise ResumeRefusedError("the request differs from the prior loop request")
        diff = self._ports.diff(request)
        digest = hashlib.sha256(diff.encode()).hexdigest() if diff else ""
        if digest != block["diff_sha256"]:
            raise ResumeRefusedError("the worktree diff changed since the prior loop")
        return receipt

    @staticmethod
    def _seed(receipt: dict[str, object]) -> _State:
        """Restore the last good turn, retaining interrupted attempts separately."""
        block = cast(dict[str, object], receipt["resume"])
        good_turn = cast(int, block["last_good_turn"])
        turns = cast(list[dict[str, object]], receipt["turns"])
        result = cast(dict[str, object], receipt["result"])
        state = _State(
            turns=[t for t in turns if cast(int, t["turn"]) <= good_turn],
            history=list(cast(list[str], block["history"])),
            refusals=cast(int, result["refusals"]),
            summary=cast(str, result["summary"]),
            last_checks=tuple(
                ModelCheckResult.model_validate(c)
                for c in cast(list[object], receipt["checks"])
            ),
            last_failure_key=cast(str, block["last_failure_key"]),
            superseded=[
                *cast(list[dict[str, object]], receipt.get("superseded_turns", [])),
                *(t for t in turns if cast(int, t["turn"]) > good_turn),
            ],
            resumes=cast(int, receipt.get("resumes", 0)) + 1,
            good_turn=good_turn,
        )
        state.good_history_len = len(state.history)
        for turn in state.turns:
            state.replies.append(
                ModelTurnReply(
                    run_id=cast(str, turn["run_id"]),
                    ok=cast(bool, turn["ok"]),
                    model=cast(str, turn["model"]),
                    invalid_reason=cast(str, turn["invalid_reason"]),
                    tokens_in=cast(int, turn.get("tokens_in", 0)),
                    tokens_out=cast(int, turn.get("tokens_out", 0)),
                )
            )
            # Lossy: receipts retain targets/names and bounded observations,
            # but omit original content, patterns and other action arguments.
            for number, action in enumerate(
                cast(list[dict[str, object]], turn["actions"]), start=1
            ):
                arguments = (
                    {"path": action["target"]}
                    if action.get("target")
                    else ({"name": action["name"]} if action.get("name") else {})
                )
                state.calls.append(
                    _Call(
                        call_id=f"t{turn['turn']}a{number}",
                        tool=cast(str, action["tool"]),
                        arguments=arguments,
                        ok=cast(bool, action["ok"]),
                        output=cast(str, action["output"]),
                    )
                )
        return state

    def _resume_block(
        self,
        request: ModelDelegatedCodeEditRequest,
        status: EnumCodeEditStatus,
        diff: str,
        state: _State,
    ) -> dict[str, object]:
        """Resume metadata for every verdict or interruption."""
        resumable = (
            status == EnumCodeEditStatus.DELEGATE_FAILED
            and state.good_turn < request.max_turns
        )
        if resumable:
            reason = (
                f"resumable from turn {state.good_turn + 1} "
                f"({request.max_turns - state.good_turn} turns left)"
            )
        elif status == EnumCodeEditStatus.DELEGATE_FAILED:
            reason = (
                "delegate_failed with no turn left to resume into "
                f"(last good turn {state.good_turn} of {request.max_turns})"
            )
        else:
            reason = (
                f"status {status.value} is a verdict, not an interruption; "
                f"{len(state.turns)} turns used of {request.max_turns}; "
                "only a delegate_failed loop resumes"
            )
        history: list[str] = []
        size = 0
        if resumable:
            for block in reversed(state.history[: state.good_history_len]):
                if size + len(block) > 120_000:
                    break
                history.append(block)
                size += len(block)
            history.reverse()
        return {
            "resumable": resumable,
            "reason": reason,
            "last_good_turn": state.good_turn,
            "turns_used": len(state.turns),
            "max_turns": request.max_turns,
            "diff_sha256": hashlib.sha256(diff.encode()).hexdigest() if diff else "",
            "last_failure_key": state.last_failure_key,
            "history": history,
            "command": (
                f"onex code-edit run --resume {request.correlation_id} "
                f"--state-root {shlex.quote(self._ports.state_root)}, "
                "with the same delegate flags"
            )
            if resumable
            else "",
        }

    def _context(
        self, request: ModelDelegatedCodeEditRequest
    ) -> tuple[tuple[str, str], ...]:
        shown: list[tuple[str, str]] = []
        for raw in request.context_paths:
            path = normalise_path(raw)
            if path is None or path == ".":
                raise _TerminalError(
                    EnumCodeEditStatus.INFRA_ERROR,
                    f"context path {raw!r} leaves the worktree",
                )
            shown.append((path, self._ports.read_file(request, path)[:MAX_VIEW_BYTES]))
        return tuple(shown)

    def _turns(
        self,
        request: ModelDelegatedCodeEditRequest,
        manifest: tuple[tuple[str, int], ...],
        context: tuple[tuple[str, str], ...],
        state: _State,
        start_turn: int = 1,
    ) -> EnumCodeEditStatus:
        index = relevant_first(request, [path for path, _ in manifest])
        failed_delegates = 0
        for turn in range(start_turn, request.max_turns + 1):
            prompt = self._prompt(request, index, context, state, turn)
            reply = self._ports.delegate(request, prompt, RESPONSE_CONTRACT, turn)
            state.replies.append(reply)
            record: dict[str, object] = {
                "turn": turn,
                "run_id": reply.run_id,
                "ok": reply.ok,
                "model": reply.model,
                "tokens_in": reply.tokens_in,
                "tokens_out": reply.tokens_out,
                "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "invalid_reason": bound_error(reply.invalid_reason),
                "actions": [],
            }
            state.turns.append(record)
            if not reply.ok and not reply.raw_text:
                failed_delegates += 1
                state.history.append(
                    f"TURN {turn}: the delegate run failed: {reply.invalid_reason[:300]}\n"
                )
                if failed_delegates >= 2:
                    raise _TerminalError(
                        EnumCodeEditStatus.DELEGATE_FAILED,
                        bound_error(
                            "two delegate runs failed in a row: " + reply.invalid_reason
                        ),
                    )
                continue
            failed_delegates = 0
            if not reply.ok or not reply.actions:
                reason = reply.invalid_reason or "the reply carried no actions"
                state.history.append(
                    f"TURN {turn}: your reply was unusable: {reason[:300]}. "
                    "Reply with one JSON object with an actions list.\n"
                )
                state.record_good(turn)
                continue
            try:
                finished = self._apply_turn(request, turn, reply, record, state)
                if finished:
                    checks = self._run_checks(request, state)
                    self._record_finish(request, checks, state)
                    if all(c.status == "passed" for c in checks):
                        return EnumCodeEditStatus.ACCEPTED
                    state.history.append(
                        "FINISH REFUSED: these checks did not pass:\n"
                        + "".join(
                            f"- {c.name}: {c.status}\n{_cap(c.output_tail, 2_000)}\n"
                            for c in checks
                            if c.status != "passed"
                        )
                    )
                    self._progress_or_stop(request, checks, state)
            finally:
                state.record_good(turn)
        checks = self._run_checks(request, state)
        if all(c.status == "passed" for c in checks):
            return EnumCodeEditStatus.ACCEPTED
        raise _TerminalError(
            EnumCodeEditStatus.BUDGET_EXHAUSTED,
            f"the turn cap ({request.max_turns}) was reached with checks failing",
        )

    @staticmethod
    def _prompt(
        request: ModelDelegatedCodeEditRequest,
        index: list[str],
        context: tuple[tuple[str, str], ...],
        state: _State,
        turn: int,
    ) -> str:
        """The turn prompt, shrunk until it fits one argv word."""
        limit = PROMPT_CHARS
        while True:
            prompt = build_turn_prompt(
                request,
                index,
                context if turn == 1 else (),
                state.history,
                turn,
                max_chars=limit,
            )
            if len(prompt.encode("utf-8")) <= PROMPT_BYTES or limit <= 20_000:
                return prompt
            limit -= 15_000

    def _apply_turn(
        self,
        request: ModelDelegatedCodeEditRequest,
        turn: int,
        reply: ModelTurnReply,
        record: dict[str, object],
        state: _State,
    ) -> bool:
        block = [f"TURN {turn}\n"]
        finished = False
        actions_log: list[dict[str, object]] = []
        for number, action in enumerate(reply.actions, start=1):
            if action.tool == EnumCodeEditTool.FINISH:
                finished = True
                state.summary = action.summary
                observation = ModelObservation(
                    ok=True, output="finish: checks run next"
                )
            else:
                observation = self._apply(request, action, state)
            arguments = {
                key: value
                for key, value in action.model_dump(
                    mode="json", exclude={"tool"}
                ).items()
                if value
            }
            state.calls.append(
                _Call(
                    call_id=f"t{turn}a{number}",
                    tool=action.tool.value,
                    arguments=arguments,
                    ok=observation.ok,
                    output=observation.output,
                )
            )
            actions_log.append(
                {
                    "tool": action.tool.value,
                    "target": action.target,
                    "name": action.name,
                    "ok": observation.ok,
                    "output": _cap(observation.output, 2_000),
                }
            )
            shown_args = ", ".join(
                f"{k}={str(v)[:80]!r}"
                for k, v in arguments.items()
                if k not in ("content", "old_string", "new_string")
            )
            block.append(
                f"> {action.tool.value}({shown_args}) -> "
                f"{'ok' if observation.ok else 'REFUSED/ERROR'}\n{observation.output}\n"
            )
        record["actions"] = actions_log
        state.history.append("".join(block))
        return finished

    def _apply(
        self,
        request: ModelDelegatedCodeEditRequest,
        action: ModelCodeEditAction,
        state: _State,
    ) -> ModelObservation:
        tool = action.tool
        if tool == EnumCodeEditTool.RUN_CHECK:
            check = request.check_named(action.name)
            if check is None:
                state.refusals += 1
                names = ", ".join(c.name for c in request.checks)
                return ModelObservation(
                    ok=False,
                    output=f"refused: {action.name!r} is not a declared check ({names})",
                )
            result = self._ports.run_check(request, check)
            return ModelObservation(
                ok=result.status == "passed",
                output=_cap(
                    f"$ {' '.join(check.argv)}\n{result.status} (exit {result.exit_code})\n"
                    f"{result.output_tail}"
                ),
            )
        raw = action.target or ("." if tool == EnumCodeEditTool.LS else "")
        path = normalise_path(raw) if raw else "."
        if path is None:
            state.refusals += 1
            return ModelObservation(
                ok=False, output=f"refused: {raw!r} leaves the worktree"
            )
        if tool in WRITING_TOOLS and not writable(request, path):
            state.refusals += 1
            return ModelObservation(
                ok=False,
                output=f"refused: {path} is not writable "
                f"(writable: {', '.join(request.writable_globs)})",
            )
        try:
            if tool == EnumCodeEditTool.VIEW:
                text = self._ports.read_file(request, path)
                return ModelObservation(
                    ok=True, output=view_window(text, path, action.offset)
                )
            if tool == EnumCodeEditTool.LS:
                return ModelObservation(
                    ok=True, output=_cap(self._ports.list_dir(request, path))
                )
            if tool == EnumCodeEditTool.GREP:
                return ModelObservation(
                    ok=True,
                    output=_cap(self._ports.grep(request, action.pattern, path)),
                )
            if tool == EnumCodeEditTool.WRITE:
                if len(action.content.encode()) > MAX_WRITE_BYTES:
                    state.refusals += 1
                    return ModelObservation(
                        ok=False,
                        output=f"refused: content over {MAX_WRITE_BYTES} bytes",
                    )
                self._ports.write_file(request, path, action.content)
                lines = action.content.count("\n") + (
                    0 if action.content.endswith("\n") or not action.content else 1
                )
                return ModelObservation(ok=True, output=f"wrote {path} ({lines} lines)")
            if tool == EnumCodeEditTool.FORMAT:
                return self._format(request, path, state)
            # EDIT
            current = self._ports.read_file(request, path)
            old_string, new_string = action.old_string, action.new_string
            if current.count(old_string) == 0 and _LINE_PREFIX.search(old_string):
                # A local model often copies the view's "  12| " prefixes; the
                # file never carries them.
                old_string = _LINE_PREFIX.sub("", old_string)
                new_string = _LINE_PREFIX.sub("", new_string)
            occurrences = current.count(old_string)
            if occurrences != 1:
                hint = _edit_hint(current, old_string) if occurrences == 0 else ""
                return ModelObservation(
                    ok=False,
                    output=f"edit failed: old_string occurs {occurrences} times in "
                    f"{path}; it must occur exactly once.{hint}",
                )
            updated = current.replace(old_string, new_string, 1)
            self._ports.write_file(request, path, updated)
            return ModelObservation(ok=True, output=f"edited {path}")
        except WorkspacePathError as exc:
            return ModelObservation(ok=False, output=f"error: {exc}")

    def _format(
        self, request: ModelDelegatedCodeEditRequest, path: str, state: _State
    ) -> ModelObservation:
        """Run the declared formatter over one writable file, in place."""
        if not request.formatter:
            state.refusals += 1
            return ModelObservation(
                ok=False, output="refused: no formatter is declared for this task"
            )
        if path.startswith("-"):
            state.refusals += 1
            return ModelObservation(
                ok=False, output=f"refused: {path!r} reads as a flag"
            )
        self._ports.read_file(request, path)
        argv = (*request.formatter, path)
        result = self._ports.run_check(
            request, ModelDeclaredCheck(name="format", argv=argv)
        )
        return ModelObservation(
            ok=result.status == "passed",
            output=_cap(
                f"$ {' '.join(argv)}\n{result.status} (exit {result.exit_code})\n"
                f"{result.output_tail}"
            ),
        )

    @staticmethod
    def _record_finish(
        request: ModelDelegatedCodeEditRequest,
        checks: tuple[ModelCheckResult, ...],
        state: _State,
    ) -> None:
        """The finish call's recorded output is the check round it triggered."""
        finish = next(
            (
                c
                for c in reversed(state.calls)
                if c.tool == EnumCodeEditTool.FINISH.value
            ),
            None,
        )
        if finish is None:
            return
        lines = []
        for result in checks:
            declared = request.check_named(result.name)
            argv = " ".join(declared.argv) if declared is not None else result.name
            lines.append(
                f"$ {argv}\n{result.status} (exit {result.exit_code})\n"
                f"{_cap(result.output_tail, 1_500)}"
            )
        finish.ok = all(c.status == "passed" for c in checks)
        finish.output = _cap("\n".join(lines))

    def _run_checks(
        self, request: ModelDelegatedCodeEditRequest, state: _State
    ) -> tuple[ModelCheckResult, ...]:
        checks = tuple(self._ports.run_check(request, c) for c in request.checks)
        state.last_checks = checks
        if any(c.status == "infra_error" for c in checks):
            raise _TerminalError(
                EnumCodeEditStatus.INFRA_ERROR,
                "a declared check could not run: "
                + ", ".join(c.name for c in checks if c.status == "infra_error"),
            )
        return checks

    def _progress_or_stop(
        self,
        request: ModelDelegatedCodeEditRequest,
        checks: tuple[ModelCheckResult, ...],
        state: _State,
    ) -> None:
        diff = self._ports.diff(request)
        key = hashlib.sha256(
            json.dumps(
                [
                    sorted((c.name, c.status, c.fingerprint) for c in checks),
                    hashlib.sha256(diff.encode()).hexdigest(),
                ]
            ).encode()
        ).hexdigest()
        if key == state.last_failure_key:
            raise _TerminalError(
                EnumCodeEditStatus.NO_PROGRESS,
                "the same check failures over the same diff twice in a row",
            )
        state.last_failure_key = key

    def _transcript(
        self,
        request: ModelDelegatedCodeEditRequest,
        manifest: tuple[tuple[str, int], ...],
        state: _State,
        started: float,
    ) -> dict[str, object]:
        """The run as the tool_use rubric reads it (built by the scoring port)."""
        return {
            "request_text": request.task,
            "answer_text": state.summary,
            "tool_schemas": [dict(schema) for schema in TOOL_SCHEMAS],
            "calls": [
                {
                    "call_id": call.call_id,
                    "tool_name": call.tool,
                    "arguments_json": json.dumps(call.arguments, sort_keys=True),
                    "status": "ok" if call.ok else "error",
                    "output": call.output,
                }
                for call in state.calls
            ],
            "turn_count": len(state.replies),
            "engine": next(
                (reply.model for reply in reversed(state.replies) if reply.model), ""
            ),
            "wall_time_ms": int((time.monotonic() - started) * 1000),
            "workspace_files": [[path, lines] for path, lines in manifest],
            "execution_results": [
                [target, result.status == "passed"]
                for result in state.last_checks
                for declared in [request.check_named(result.name)]
                if declared is not None
                for target in declared.targets
            ],
        }


__all__ = [
    "HandlerDelegatedCodeEditOrchestrator",
    "glob_regex",
    "normalise_path",
    "relevant_first",
    "view_window",
    "writable",
]
