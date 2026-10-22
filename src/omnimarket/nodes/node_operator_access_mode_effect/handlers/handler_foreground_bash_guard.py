# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The main session's Bash calls never stop on a prompt and never wait (shape rules, OMN-20893).

Shape rules, ported unchanged from the host's untracked ``foreground-bash-shape-guard.py``
(operator, 2026-10-08: "Why do you keep running bash calls that require permission? It locks the
foreground", then "Yes, do both"), for every main-session call:

* chaining outside quotes: ``;``, ``&&``, ``||``, a background ``&``, or a second line;
* ``cd`` or ``pushd`` as a command word (use absolute paths or ``git -C``);
* ``sleep`` as a command word; ``ssh`` or ``scp`` as a command word (lab probes belong to a lane
  or to ``/omni:lab-run``, whose ``--host`` reaches one named host).

RULING 2026-10-13T04:14:36Z lane=orchestrator-31d9 (OMN-20893, "So the hooks need fixing")
narrows the shape rules in two ways, and only in these two:

* A detached launch passes in every permission mode: one command whose only chaining operator is
  a single trailing ``&``, whose first command word (after env assignments) is ``nohup``, and
  whose stdout and stderr both go to a file (``> f 2>&1``, ``&> f`` or ``>f 2>f``). It is the one
  shell form that detaches, and the wait rule below sends a long job to the background.
* When the session's permission mode is ``bypassPermissions`` (and only then), ``;``, ``&&``,
  ``||`` and a second line are not refused, because nothing can stop on a prompt. A bare ``&``
  that is not the detached form above stays refused in every mode.

``cd``, ``pushd``, ``sleep``, ``ssh``, ``scp`` and every wait rule are unchanged in every mode, and
in a chain they are checked for every command of the chain, not only the first.

Wait rules (OMN-20893, RULING 2026-10-10T16:55:54Z: always on, in both access modes), for a
main-session call in an operator-facing session:

* ``wait`` or ``watch`` as a command word, ``tail -f`` / ``-F`` / ``--follow``;
* a ``--watch`` flag anywhere (``gh pr checks --watch``), ``gh run watch``, ``kubectl ... -w``;
* ``timeout N`` wrapping a command for more than two minutes;
* a Bash ``timeout`` parameter above two minutes.

Each refusal names the background Workflow as the place to wait. Subagent and workflow-lane calls
(``agent_id`` set) pass, and a headless session's calls are held to the shape rules only. Text
inside quotes is never parsed as shell.

RULING 2026-10-10T17:54:29Z also sends fetching to background lanes. In an operator-facing
main session, curl, wget, git fetch/pull/clone and the WebFetch/WebSearch tools are refused.
Local reads and a fully detached fetch remain available.
"""

from __future__ import annotations

import re
import shlex
from typing import NamedTuple

from ..models.model_foreground_bash_request import ModelForegroundBashRequest
from ..models.model_hook_verdict import ModelHookVerdict

REFUSED_WORDS = {
    "cd": "use absolute paths or git -C instead of cd",
    "pushd": "use absolute paths or git -C instead of pushd",
    "sleep": "do not wait in the foreground; hand waiting to a lane or a background Workflow",
    "ssh": (
        "run lab probes from a lane or through /omni:lab-run"
        " (its --host option runs one command on one named host), not the foreground"
    ),
    "scp": "run lab copies from a lane, not the foreground",
}
PREFIX_WORDS = {"env", "time", "command", "nohup", "exec"}
BYPASS_MODE = "bypassPermissions"
BYPASS_CHAIN_OPS = frozenset({";", "&&", "||", "newline"})
DETACHED_FORM = "nohup <command> > <file> 2>&1 &"
SHAPE_GUARD = "operator_access_mode_foreground_bash_shape"
WAIT_GUARD = "operator_access_mode_foreground_wait"
FETCH_GUARD = "operator_access_mode_foreground_fetch"
FETCH_TOOLS = frozenset({"WebFetch", "WebSearch"})
FETCH_REASON = (
    "BLOCKED (foreground fetch): fetching belongs to a background lane or Workflow;"
    " delegate the fetch and keep the foreground free for the operator."
)
HEAD = "BLOCKED (foreground Bash shape): "
TAIL = " Read files with the Read tool; give Bash one plain command with literal paths."

WAIT_HEAD = "BLOCKED (foreground wait, OMN-20893): "
WAIT_TAIL = (
    " The main session never blocks on a wait: launch a background Workflow that waits and"
    " reports back, and keep the foreground free for the operator."
)
WAIT_LIMIT_SECONDS = 120
TEXT_TOOLS = frozenset({"grep", "egrep", "rg", "echo", "printf", "sed", "awk", "cat"})
WAIT_WORDS = {
    "wait": "the shell wait builtin blocks the foreground",
    "watch": "watch re-runs a command until interrupted",
}


class Scan(NamedTuple):
    ops: list[str]  # chaining operators found outside quotes
    words: list[
        str
    ]  # the command word of every command, after env assignments and prefix words
    segments: list[
        str
    ]  # the text of every command, split at each operator, pipe and ``(``


def scan(command: str) -> Scan:
    """Scan one command line outside quotes."""
    ops: list[str] = []
    words: list[str] = []
    segments: list[str] = []
    seg_start = 0
    expect_word = True
    word = ""
    i, n = 0, len(command)
    quote = ""

    def cut(at: int, width: int) -> None:
        nonlocal seg_start
        segments.append(command[seg_start:at])
        seg_start = at + width

    def end_word() -> None:
        nonlocal word, expect_word
        if word:
            if expect_word:
                if "=" in word and not word.startswith("="):
                    pass  # an env assignment; the command word is still to come
                elif word in PREFIX_WORDS:
                    pass
                else:
                    words.append(word)
                    expect_word = False
            word = ""

    while i < n:
        c = command[i]
        if quote == "'":
            if c == "'":
                quote = ""
            word += c
            i += 1
            continue
        if quote == '"':
            if c == "\\" and i + 1 < n:
                word += command[i : i + 2]
                i += 2
                continue
            if c == '"':
                quote = ""
            word += c
            i += 1
            continue
        if c in "'\"":
            quote = c
            word += c
            i += 1
            continue
        if c == "\\" and i + 1 < n:
            if command[i + 1] == "\n":
                i += 2  # a line continuation is still one command
                continue
            word += command[i : i + 2]
            i += 2
            continue
        if c in " \t":
            end_word()
            i += 1
            continue
        if c == "\n":
            end_word()
            if command[i + 1 :].strip():
                ops.append("newline")
            cut(i, 1)
            expect_word = True
            i += 1
            continue
        two = command[i : i + 2]
        if two in ("&&", "||"):
            end_word()
            ops.append(two)
            cut(i, 2)
            expect_word = True
            i += 2
            continue
        if c == ";":
            end_word()
            ops.append(";")
            cut(i, 1)
            expect_word = True
            i += 1
            continue
        if c == "&":
            if two == "&>" or (i > 0 and command[i - 1] in "<>"):
                word += c  # a redirection such as &> or 2>&1
                i += 1
                continue
            end_word()
            ops.append("&")
            cut(i, 1)
            expect_word = True
            i += 1
            continue
        if c == "|":
            end_word()
            cut(i, 1)
            expect_word = True
            i += 1
            continue
        if c == "(":
            end_word()
            cut(i, 1)
            expect_word = True
            i += 1
            continue
        word += c
        i += 1
    end_word()
    cut(n, 0)
    return Scan(
        ops, words, [seg.strip().rstrip(")").strip() for seg in segments if seg.strip()]
    )


# ``[fd]>[>]target``, ``&>[>]target``; the target is empty when it is the next token
_REDIRECT = re.compile(r"^(?P<fd>[0-9]*|&)>>?(?P<target>.*)$")


def _files_for_both_streams(tokens: list[str]) -> bool:
    """True when, after the redirections in order, stdout and stderr both go to a file."""
    streams = {1: "", 2: ""}
    i = 0
    while i < len(tokens):
        match = _REDIRECT.match(tokens[i])
        i += 1
        if not match:
            continue
        target = match.group("target")
        if not target:
            if i >= len(tokens):
                return False
            target = tokens[i]
            i += 1
        fd = match.group("fd")
        fds = [1, 2] if fd == "&" else [int(fd or 1)]
        for n in (n for n in fds if n in streams):
            if target.startswith(
                "&"
            ):  # 2>&1: the stream copies what the other one now is
                source = target[1:]
                streams[n] = streams[int(source)] if source in ("1", "2") else ""
            else:
                streams[n] = target
    return bool(streams[1] and streams[2])


def is_detached_launch(command: str, scanned: Scan) -> bool:
    """One ``nohup`` command, a single trailing ``&``, stdout and stderr both sent to a file."""
    body = command.rstrip()
    if scanned.ops != ["&"] or len(scanned.words) != 1 or not body.endswith("&"):
        return False
    try:
        tokens = shlex.split(body[:-1], posix=False)
    except ValueError:
        return False
    while tokens and re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0]):
        tokens = tokens[1:]
    return bool(tokens) and tokens[0] == "nohup" and _files_for_both_streams(tokens[1:])


def shape_problems(command: str, permission_mode: str = "") -> list[str]:
    scanned = scan(command)
    allowed_ops: frozenset[str] = frozenset()
    if is_detached_launch(command, scanned):
        allowed_ops = frozenset({"&"})
    elif permission_mode == BYPASS_MODE:
        allowed_ops = BYPASS_CHAIN_OPS
    ops = set(scanned.ops) - allowed_ops
    problems: list[str] = []
    if ops:
        problems.append(
            "chained commands ("
            + ", ".join(sorted(ops))
            + "); make one call per command"
        )
    if "&" in ops:
        problems.append(
            f"a background & is allowed only as one detached launch: {DETACHED_FORM}"
        )
    for w in scanned.words:
        base = w.rsplit("/", 1)[-1]
        if base in REFUSED_WORDS and REFUSED_WORDS[base] not in problems:
            problems.append(REFUSED_WORDS[base])
    return problems


def _duration_seconds(text: str) -> float | None:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not match:
        return None
    scale = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return float(match.group(1)) * scale


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _options(tokens: list[str]) -> list[str]:
    """The option tokens of one command, up to a ``--`` terminator."""
    found: list[str] = []
    for token in tokens:
        if token == "--":
            break
        if token.startswith("-"):
            found.append(token)
    return found


def _follows(options: list[str]) -> bool:
    for option in options:
        if option.startswith("--follow"):
            return True
        if not option.startswith("--") and re.fullmatch(
            r"-[A-Za-z0-9]*[fF][A-Za-z0-9]*", option
        ):
            return True
    return False


def _timeout_duration(tokens: list[str]) -> str | None:
    """The DURATION operand of ``timeout [OPTION]... DURATION COMMAND``."""
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "--":
            return tokens[i + 1] if i + 1 < len(tokens) else None
        if token in {"-s", "--signal", "-k", "--kill-after"}:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return None


def _stage_problems(stage: str) -> list[str]:
    tokens = _tokens(stage)
    while tokens and (
        re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0]) or tokens[0] in PREFIX_WORDS
    ):
        tokens = tokens[1:]
    if not tokens:
        return []
    name = tokens[0].rsplit("/", 1)[-1]
    rest = tokens[1:]
    if name in TEXT_TOOLS:
        return []  # a search or print of the words, not a run of them
    problems: list[str] = []
    if name in WAIT_WORDS:
        problems.append(WAIT_WORDS[name])
    options = _options(rest)
    if any(t == "--watch" or t.startswith("--watch=") for t in options):
        problems.append("a --watch flag polls until the thing finishes")
    if name == "gh" and rest[:2] == ["run", "watch"]:
        problems.append("gh run watch polls until the run finishes")
    if name == "kubectl" and "-w" in options:
        problems.append("kubectl -w streams until interrupted")
    if name == "tail" and _follows(options):
        problems.append("tail -f follows a file until interrupted")
    if name == "timeout":
        duration = _timeout_duration(rest)
        seconds = _duration_seconds(duration) if duration else None
        if seconds is not None and seconds > WAIT_LIMIT_SECONDS:
            problems.append(f"timeout {duration} holds the foreground past two minutes")
        wrapped = rest[rest.index(duration) + 1 :] if duration in rest else []
        if wrapped:
            problems.extend(
                p for p in _stage_problems(shlex.join(wrapped)) if p not in problems
            )
    return problems


def wait_problems(command: str, timeout_ms: int | None) -> list[str]:
    problems: list[str] = []
    for stage in scan(command).segments:
        for problem in _stage_problems(stage):
            if problem not in problems:
                problems.append(problem)
    if timeout_ms is not None and timeout_ms > WAIT_LIMIT_SECONDS * 1000:
        problems.append(
            f"a Bash timeout of {timeout_ms // 1000} s holds the foreground past two minutes"
        )
    return problems


def _fetch_stage(stage: str) -> bool:
    tokens = _tokens(stage)
    while tokens and (
        re.match(r"^[A-Za-z_][A-Za-z_0-9]*=", tokens[0]) or tokens[0] in PREFIX_WORDS
    ):
        tokens = tokens[1:]
    if not tokens:
        return False
    name = tokens[0].rsplit("/", 1)[-1]
    rest = tokens[1:]
    if name in {"curl", "wget"}:
        return True
    if name == "timeout":
        duration = _timeout_duration(rest)
        wrapped = rest[rest.index(duration) + 1 :] if duration in rest else []
        return bool(wrapped) and _fetch_stage(shlex.join(wrapped))
    if name != "git":
        return False
    # Git's global options precede the subcommand; option operands are not command words.
    i = 0
    while i < len(rest):
        word = rest[i]
        if word in {
            "-C",
            "-c",
            "--git-dir",
            "--work-tree",
            "--namespace",
            "--config-env",
        }:
            i += 2
        elif word == "--":
            i += 1
            break
        elif word.startswith("-"):
            i += 1
        else:
            break
    return i < len(rest) and rest[i] in {"fetch", "pull", "clone"}


class HandlerForegroundBashGuard:
    """Refuse a main-session Bash call that can prompt, chain or wait."""

    def handle(self, request: ModelForegroundBashRequest) -> ModelHookVerdict:
        if not request.main_session:
            return ModelHookVerdict.allow()
        if request.tool_name in FETCH_TOOLS:
            return (
                ModelHookVerdict.refuse(FETCH_REASON, FETCH_GUARD, "foreground-fetch")
                if request.operator_facing
                else ModelHookVerdict.allow()
            )
        shape = shape_problems(request.command, request.permission_mode)
        if shape:
            return ModelHookVerdict.refuse(
                HEAD + "; ".join(shape) + "." + TAIL,
                SHAPE_GUARD,
                "foreground-bash-shape",
            )
        if request.operator_facing:
            if not is_detached_launch(request.command, scan(request.command)) and any(
                _fetch_stage(stage) for stage in scan(request.command).segments
            ):
                return ModelHookVerdict.refuse(
                    FETCH_REASON, FETCH_GUARD, "foreground-fetch"
                )
            waits = wait_problems(request.command, request.timeout_ms)
            if waits:
                return ModelHookVerdict.refuse(
                    WAIT_HEAD + "; ".join(waits) + "." + WAIT_TAIL,
                    WAIT_GUARD,
                    "foreground-wait",
                )
        return ModelHookVerdict.allow()
