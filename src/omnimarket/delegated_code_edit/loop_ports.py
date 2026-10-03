# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real ports of the delegated code edit loop (OMN-20290).

* delegate -> the sanctioned ``onex`` wrapper, ``onex delegate <prompt>
              --task-type code_generation --response-contract <schema>`` with
              the caller's bus and locus flags, its lane and its ticket. Each
              turn is one delegation run with its own receipt under
              ``<state root>/runs/<run id>/``, exactly as any other caller's.
* workspace -> the git worktree named by the request, every path resolved and
              checked to stay inside it (a symlink cannot carry a read or a
              write out).
* run_check -> the declared argv, run in the worktree with no shell and a
              timeout.
* score    -> the tool_use rubric over the recorded calls, through
              ``node_delegation_rubric_check_compute``.

The loop receipt is ``<state root>/runs/<correlation id>/loop_receipt.json``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.delegation.rubric.contract_loader import (
    load_delegation_class_rubrics,
)
from omnimarket.delegation.rubric.tool_use_record import score as score_request
from omnimarket.delegation.rubric.tool_use_transcript import (
    TOOL_USE_CLASS,
    declared_tools_from_schemas,
    recorded_calls_request,
)
from omnimarket.nodes.node_delegated_code_edit_orchestrator import (
    LoopReceiptExistsError,
    ModelCheckResult,
    ModelDeclaredCheck,
    ModelDelegatedCodeEditRequest,
    ModelTurnReply,
    ResumeRefusedError,
    WorkspacePathError,
    bound_error,
    parse_turn_reply,
)
from omnimarket.nodes.node_delegation_rubric_check_compute.models import (
    EnumToolCallStatus,
    ModelRubricExecutionResult,
    ModelToolCall,
    ModelToolCallResult,
    ModelWorkspaceFile,
)

_DELEGATE_TIMEOUT_SECONDS = 900
#: Linux bounds one argv word at 128 KiB; the prompt travels as one.
MAX_PROMPT_BYTES = 120_000
_MAX_MANIFEST_FILES = 50_000
_MAX_COUNTED_BYTES = 1_000_000
_MAX_GREP_LINES = 200
_OUTPUT_TAIL = 6_000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_VOLATILE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:s|ms|sec|seconds)\b|0x[0-9a-fA-F]+")

#: The first slice runs the delegate orchestrator in this process; only the
#: model call leaves the machine.
IN_PROCESS_DELEGATE_FLAGS: tuple[str, ...] = (
    "--bus",
    "inmemory",
    "--locus",
    "in-process",
)


def deployed_lane_delegate_flags(lane: str) -> tuple[str, ...]:
    """Delegate turns go over the bus to the lane's deployed orchestrator."""
    return ("--bus", "kafka", "--lane", lane, "--locus", "deployed-lane")


def _git_env() -> dict[str, str]:
    """The environment without inherited git location variables: a GIT_DIR from
    a calling hook would otherwise redirect every git call to the wrong repo."""
    return scrub_git_location_env(os.environ)


def _check_env() -> dict[str, str]:
    """A check's environment: no git location variables, and no VIRTUAL_ENV of
    the process running the loop, so the worktree's own project environment is
    the one a check uses."""
    env = _git_env()
    env.pop("VIRTUAL_ENV", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_subprocess(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=_DELEGATE_TIMEOUT_SECONDS,
        check=False,
    )


def check_fingerprint(exit_code: int | None, output: str) -> str:
    """A failure identity that ignores timings and addresses."""
    return hashlib.sha256(
        f"{exit_code}\0{_VOLATILE.sub('', output)}".encode()
    ).hexdigest()


@dataclass(frozen=True)
class _DelegateReceipt:
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    failure: str = ""


def _read_delegate_receipt(path: Path) -> _DelegateReceipt:
    """Read delegate metrics and failure details, tolerating malformed receipts."""

    def block(value: object) -> dict[str, object]:
        return cast("dict[str, object]", value) if isinstance(value, dict) else {}

    def tokens(value: object) -> int:
        if not isinstance(value, (str, int, float)):
            return 0
        try:
            return int(value)
        except (ValueError, OverflowError):
            return 0

    try:
        receipt = block(json.loads(path.read_text()))
    except (OSError, ValueError, UnicodeError, RecursionError):
        return _DelegateReceipt()
    result = block(block(receipt.get("receipt")).get("result"))
    # Deployed-lane metrics are in the payload; in-process metrics are in result.
    terminal = block(block(result.get("terminal_payload")).get("payload", result))
    metrics = block(terminal.get("metrics"))
    failures = [
        receipt.get("status") if receipt.get("status") != "success" else "",
        receipt.get("terminal_failure_cause"),
        receipt.get("terminal_failure_reason"),
        receipt.get("failure_reason"),
        result.get("error"),
        result.get("runtime_error_type"),
    ]
    if "terminal_payload" in result and result["terminal_payload"] is None:
        failures.append("terminal_payload is null")
    return _DelegateReceipt(
        model=str(receipt.get("model") or ""),
        tokens_in=tokens(metrics.get("input_tokens")),
        tokens_out=tokens(metrics.get("output_tokens")),
        failure="; ".join(
            text
            for value in failures
            if value and (text := " ".join(str(value).split()))
        ),
    )


class DelegatedCodeEditPorts:
    """Binds the code edit loop's ports to onex delegate, the worktree and the checks."""

    def __init__(
        self,
        *,
        onex: Path,
        state_root: Path,
        delegate_flags: tuple[str, ...] = IN_PROCESS_DELEGATE_FLAGS,
        run_delegate: Callable[[list[str]], subprocess.CompletedProcess[str]]
        | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._onex = onex
        self._state_root = state_root
        self._delegate_flags = delegate_flags
        self._run_delegate = run_delegate or _run_subprocess
        self._max_tokens = max_tokens

    # -- receipt --------------------------------------------------------------

    def _loop_dir(self, loop_run_id: str) -> Path:
        return self._state_root / "runs" / loop_run_id

    @property
    def state_root(self) -> str:
        """The receipt root for the resume command."""
        return str(self._state_root)

    def load_loop_receipt(self, loop_run_id: str) -> dict[str, object] | None:
        """Read the current receipt, tolerating missing or unreadable JSON."""
        try:
            payload = json.loads(
                (self._loop_dir(loop_run_id) / "loop_receipt.json").read_text()
            )
        except (OSError, ValueError, UnicodeError, RecursionError):
            return None
        return cast("dict[str, object]", payload) if isinstance(payload, dict) else None

    def claim_loop_receipt(self, loop_run_id: str, *, resume: bool = False) -> None:
        """Claim the correlation id with an exclusive create, so two concurrent
        runs of one id cannot both pass: the second finds the claim and is refused.
        A run that dies after claiming leaves the claim; rerun with a new id."""
        loop_dir = self._loop_dir(loop_run_id)
        loop_dir.mkdir(parents=True, exist_ok=True)
        receipt = loop_dir / "loop_receipt.json"
        if not resume and receipt.exists():
            raise LoopReceiptExistsError(
                f"loop {loop_run_id} is already claimed and has a receipt"
            )
        claim = loop_dir / "loop_claim"
        try:
            with claim.open("x") as handle:
                handle.write(f"{os.getpid()}\n")
        except FileExistsError as exc:
            raise LoopReceiptExistsError(
                f"loop {loop_run_id} is already claimed"
                + (
                    " and has a receipt"
                    if (loop_dir / "loop_receipt.json").exists()
                    else ""
                )
            ) from exc
        if resume:
            try:
                if not receipt.exists():
                    raise ResumeRefusedError(
                        f"loop {loop_run_id} has no receipt to resume"
                    )
                number = 1 + len(list(loop_dir.glob("loop_receipt.*.json")))
                os.replace(receipt, loop_dir / f"loop_receipt.{number}.json")
            except Exception:
                claim.unlink(missing_ok=True)
                raise
            return
        if receipt.exists():
            claim.unlink(missing_ok=True)
            raise LoopReceiptExistsError(
                f"loop {loop_run_id} is already claimed and has a receipt"
            )

    def write_loop_receipt(self, loop_run_id: str, payload: dict[str, object]) -> None:
        path = self._loop_dir(loop_run_id) / "loop_receipt.json"
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=".loop_receipt.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(json.dumps(payload, indent=1, sort_keys=True) + "\n")
            os.replace(temporary, path)
        finally:
            (path.parent / "loop_claim").unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    # -- workspace ------------------------------------------------------------

    @staticmethod
    def _root(request: ModelDelegatedCodeEditRequest) -> Path:
        root = Path(request.workspace_root).resolve()
        if not root.is_dir():
            raise WorkspacePathError(f"{request.workspace_root} is not a directory")
        return root

    def _inside(self, request: ModelDelegatedCodeEditRequest, rel: str) -> Path:
        root = self._root(request)
        candidate = (root / rel).resolve()
        if candidate != root and root not in candidate.parents:
            raise WorkspacePathError(f"{rel} resolves outside the worktree")
        if ".git" in candidate.relative_to(root).parts:
            raise WorkspacePathError(f"{rel} is inside .git")
        return candidate

    def workspace_files(
        self, request: ModelDelegatedCodeEditRequest
    ) -> tuple[tuple[str, int], ...]:
        root = self._root(request)
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-co", "--exclude-standard", "-z"],
            capture_output=True,
            env=_git_env(),
            check=False,
        )
        if listed.returncode != 0:
            raise WorkspacePathError(
                f"{root} is not a git worktree: {listed.stderr.decode()[-200:]}"
            )
        rows: list[tuple[str, int]] = []
        for raw in listed.stdout.split(b"\0"):
            if not raw:
                continue
            rel = raw.decode("utf-8", "replace")
            path = root / rel
            lines = 0
            try:
                if path.is_file() and not path.is_symlink():
                    data = path.read_bytes()[:_MAX_COUNTED_BYTES]
                    lines = (
                        0
                        if b"\0" in data
                        else data.count(b"\n")
                        + (1 if data and not data.endswith(b"\n") else 0)
                    )
            except OSError:
                lines = 0
            rows.append((rel, lines))
            if len(rows) >= _MAX_MANIFEST_FILES:
                break
        return tuple(sorted(rows))

    def read_file(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        target = self._inside(request, path)
        if not target.is_file():
            raise WorkspacePathError(f"{path} is not a file")
        return target.read_text(encoding="utf-8", errors="replace")

    def list_dir(self, request: ModelDelegatedCodeEditRequest, path: str) -> str:
        target = self._inside(request, path)
        if not target.is_dir():
            raise WorkspacePathError(f"{path} is not a directory")
        names = sorted(
            entry.name + ("/" if entry.is_dir() else "")
            for entry in target.iterdir()
            if entry.name != ".git"
        )
        return "\n".join(names) or "(empty)"

    def grep(
        self, request: ModelDelegatedCodeEditRequest, pattern: str, path: str
    ) -> str:
        target = self._inside(request, path or ".")
        if not target.exists():
            # git grep answers a missing pathspec with "no matches", which told a
            # model searching the wrong layout nothing (OMN-20291, ab8d7ef6).
            raise WorkspacePathError(f"{path} does not exist")
        root = self._root(request)
        found = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "grep",
                "-n",
                "-I",
                "-E",
                "--untracked",
                "--no-color",
                "-e",
                pattern,
                "--",
                str(target.relative_to(root) or "."),
            ],
            capture_output=True,
            env=_git_env(),
            text=True,
            check=False,
            timeout=60,
        )
        if found.returncode == 1:
            return "no matches"
        if found.returncode != 0:
            raise WorkspacePathError(f"grep failed: {found.stderr.strip()[-200:]}")
        lines = found.stdout.splitlines()
        more = len(lines) - _MAX_GREP_LINES
        shown = "\n".join(lines[:_MAX_GREP_LINES])
        return shown + (f"\n... [{more} more matching lines]" if more > 0 else "")

    def write_file(
        self, request: ModelDelegatedCodeEditRequest, path: str, content: str
    ) -> None:
        target = self._inside(request, path)
        if target.is_symlink() or target.is_dir():
            raise WorkspacePathError(f"{path} is a symlink or a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Re-check after mkdir: a parent created through a symlink is caught here.
        self._inside(request, path)
        target.write_text(content, encoding="utf-8")

    def diff(self, request: ModelDelegatedCodeEditRequest) -> str:
        """Tracked changes against HEAD, then each untracked file as an addition.

        Read only: the index is never touched.
        """
        root = self._root(request)
        tracked = subprocess.run(
            ["git", "-C", str(root), "diff", "--no-color", "HEAD"],
            capture_output=True,
            env=_git_env(),
            text=True,
            check=False,
        ).stdout
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-o", "--exclude-standard", "-z"],
            capture_output=True,
            env=_git_env(),
            check=False,
        ).stdout.split(b"\0")
        parts = [tracked]
        for raw in sorted(u for u in untracked if u):
            rel = raw.decode("utf-8", "replace")
            # --no-index exits 1 when the files differ, which they always do here.
            added = subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "diff",
                    "--no-color",
                    "--no-index",
                    "--",
                    "/dev/null",
                    rel,
                ],
                capture_output=True,
                env=_git_env(),
                text=True,
                check=False,
            ).stdout
            parts.append(added)
        return "".join(parts)

    # -- checks ---------------------------------------------------------------

    def run_check(
        self, request: ModelDelegatedCodeEditRequest, check: ModelDeclaredCheck
    ) -> ModelCheckResult:
        root = self._root(request)
        started = time.monotonic()
        try:
            done = subprocess.run(
                list(check.argv),
                cwd=root,
                capture_output=True,
                text=True,
                timeout=check.timeout_seconds,
                check=False,
                env=_check_env(),
            )
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            return ModelCheckResult(
                name=check.name,
                status="timeout",
                output_tail=out[-_OUTPUT_TAIL:],
                fingerprint=check_fingerprint(None, "timeout"),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except OSError as exc:
            return ModelCheckResult(
                name=check.name,
                status="infra_error",
                output_tail=str(exc)[:_OUTPUT_TAIL],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        output = _ANSI.sub("", done.stdout + done.stderr)[-_OUTPUT_TAIL:]
        return ModelCheckResult(
            name=check.name,
            status="passed" if done.returncode == 0 else "failed",
            exit_code=done.returncode,
            output_tail=output,
            fingerprint=check_fingerprint(done.returncode, output),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # -- delegate -------------------------------------------------------------

    def delegate(
        self,
        request: ModelDelegatedCodeEditRequest,
        prompt: str,
        response_contract: dict[str, object],
        turn: int,
    ) -> ModelTurnReply:
        if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            return ModelTurnReply(
                run_id="",
                ok=False,
                invalid_reason=f"the turn prompt is over {MAX_PROMPT_BYTES} bytes",
            )
        argv = [
            str(self._onex),
            "delegate",
            prompt,
            "--task-type",
            request.task_type,
            "--response-contract",
            json.dumps(response_contract, separators=(",", ":")),
            *self._delegate_flags,
            "--state-root",
            str(self._state_root),
        ]
        if self._max_tokens is not None:
            argv += ["--max-tokens", str(self._max_tokens)]
        if request.caller_lane:
            argv += ["--caller-lane", request.caller_lane]
        if request.ticket:
            argv += ["--ticket", request.ticket]
        try:
            result = self._run_delegate(argv)
        except subprocess.TimeoutExpired:
            return ModelTurnReply(
                run_id="",
                ok=False,
                invalid_reason=f"onex delegate ran past {_DELEGATE_TIMEOUT_SECONDS} s",
            )
        run_id = ""
        for line in reversed(result.stdout.splitlines()):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict) and payload.get("run_id"):
                run_id = str(payload["run_id"])
                break
        if not run_id:
            match = re.search(r"runs/([0-9a-f-]{36})/receipt\.json", result.stderr)
            run_id = match.group(1) if match else ""
        text = ""
        run_dir = self._state_root / "runs" / run_id
        if run_id and (run_dir / "result.txt").is_file():
            text = (run_dir / "result.txt").read_text()
        receipt = (
            _read_delegate_receipt(run_dir / "receipt.json")
            if run_id
            else _DelegateReceipt()
        )
        if not text:
            reason = (
                f"onex delegate exited {result.returncode}: {result.stderr.rstrip()}"
                if result.returncode != 0
                else f"onex delegate run {run_id or '?'} returned no result text"
            )
            if receipt.failure:
                reason += f" | receipt: {receipt.failure}"
            return ModelTurnReply(
                run_id=run_id,
                ok=False,
                tokens_in=receipt.tokens_in,
                tokens_out=receipt.tokens_out,
                model=receipt.model,
                invalid_reason=bound_error(reason),
            )
        actions, reason = parse_turn_reply(text)
        return ModelTurnReply(
            run_id=run_id,
            ok=not reason,
            actions=actions,
            invalid_reason=reason,
            raw_text=text[:200_000],
            tokens_in=receipt.tokens_in,
            tokens_out=receipt.tokens_out,
            model=receipt.model,
        )

    # -- score ----------------------------------------------------------------

    def score(
        self,
        request: ModelDelegatedCodeEditRequest,
        transcript: dict[str, object],
    ) -> dict[str, object]:
        return score_transcript(transcript, run_ref=request.correlation_id)


def score_transcript(
    transcript: dict[str, object], *, run_ref: str
) -> dict[str, object]:
    """The tool_use verdict record of one loop transcript (the handler's shape)."""
    rubric = load_delegation_class_rubrics().for_class(TOOL_USE_CLASS)
    schemas = transcript.get("tool_schemas")
    calls_raw = transcript.get("calls")
    files_raw = transcript.get("workspace_files")
    results_raw = transcript.get("execution_results")
    calls: list[ModelToolCall] = []
    for row in calls_raw if isinstance(calls_raw, list) else []:
        calls.append(
            ModelToolCall(
                call_id=str(row["call_id"]),
                tool_name=str(row["tool_name"]),
                arguments_json=str(row["arguments_json"]),
                result=ModelToolCallResult(
                    status=EnumToolCallStatus(str(row["status"])),
                    output=str(row["output"]),
                ),
                refused=bool(row.get("refused", False)),
            )
        )
    workspace: Sequence[ModelWorkspaceFile] | None = (
        [ModelWorkspaceFile(path=str(p), line_count=int(n)) for p, n in files_raw]
        if isinstance(files_raw, list)
        else None
    )
    request = recorded_calls_request(
        rubric=rubric,
        request_text=str(transcript.get("request_text", "")),
        answer_text=str(transcript.get("answer_text", "")),
        declared_tools=declared_tools_from_schemas(
            schemas if isinstance(schemas, list) else []
        ),
        calls=calls,
        turn_count=int(str(transcript.get("turn_count", 0))),
        wall_time_ms=int(str(transcript.get("wall_time_ms", 0))),
        workspace_files=workspace,
        engine=str(transcript.get("engine") or "") or None,
        execution_results=[
            ModelRubricExecutionResult(target=str(t), passed=bool(p))
            for t, p in (results_raw if isinstance(results_raw, list) else [])
        ],
    )
    return score_request(request, "delegated-code-edit", run_ref)


__all__ = [
    "IN_PROCESS_DELEGATE_FLAGS",
    "MAX_PROMPT_BYTES",
    "DelegatedCodeEditPorts",
    "check_fingerprint",
    "deployed_lane_delegate_flags",
    "score_transcript",
]
