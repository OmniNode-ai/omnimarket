# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one entry every operator-supervision hook calls, and the ``onex-access-mode`` switch.

Registered by ``onex-user-hooks-install`` in the user settings file, run as
``python -m ...handler_operator_hook_cli <event>`` with the payload on stdin:

* ``session-start`` (SessionStart): resolve and record the access mode. For a session that opted in
  to session start (OMN-21149), also tell the model which mode it is in and, in REMOTE mode, run the
  setup check (OMN-20895). Never blocks.
* ``prompt`` (UserPromptSubmit): record the time of the operator's prompt (the push gate's idle
  clock), and take the prompt commands ``mode local``, ``mode remote``, ``mode auto`` and ``mode``.
  Record ``Silence tick`` turns and take ``silent on|off|show``. Never blocks.
* ``pre-tool`` (PreToolUse on Bash, WebFetch, WebSearch, PushNotification, SendUserMessage,
  Workflow and Agent):
  the foreground Bash and fetch guard, the attention-only push gate (OMN-20896) and the reply
  check on a sent message (OMN-20894), plus the lab dispatch gate in every session and lane.
  A refusal is exit 2 with the reason on stderr.
* ``stop`` (Stop): the silence check on all text blocks in the turn, then the reply check on
  the turn's last message (OMN-20894). A refusal asks the
  model to rewrite once; a second stop in the same turn (``stop_hook_active``) passes, so a
  refusal can never loop. The repeated-reply check (OMN-20893) is the exception: it reads the
  session transcript and runs on every stop, because a re-firing goal hook sets
  ``stop_hook_active`` on every turn. An empty reply always passes, so it never forces a reply.

``onex-access-mode show|set local|set remote|set auto [--session ID]`` reads or sets the override:
without ``--session`` it is global (every operator-facing session), with it one session.
``silent on|off [--session ID]`` sets silent mode with the same scope.

An internal error never wedges a session: the hook prints a warning and lets the action through.
Standard library only, so the installed copy runs under the host Python with no venv.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..models.enum_access_mode import EnumAccessMode
from ..models.model_access_mode_request import ModelAccessModeRequest
from ..models.model_access_mode_result import ModelAccessModeResult
from ..models.model_foreground_bash_request import ModelForegroundBashRequest
from ..models.model_hook_verdict import ModelHookVerdict
from ..models.model_lab_dispatch_request import ModelLabDispatchRequest
from ..models.model_operator_reply_request import ModelOperatorReplyRequest
from ..models.model_push_gate_request import ModelPushGateRequest
from ..models.model_remote_setup_request import ModelRemoteSetupRequest
from ..models.model_silence_request import ModelSilenceRequest
from .handler_access_mode_detect import HandlerAccessModeDetect
from .handler_access_mode_state import (
    prune_stale_sessions,
    read_global_override,
    read_global_silent,
    read_session,
    state_dir,
    update_session,
    write_global_override,
    write_global_silent,
)
from .handler_attention_push_gate import ATTENTION_KINDS, HandlerAttentionPushGate
from .handler_foreground_bash_guard import FETCH_TOOLS, HandlerForegroundBashGuard
from .handler_lab_dispatch_gate import (
    HandlerLabDispatchGate,
    mac_route_count,
    read_lab_pool,
)
from .handler_operator_reply_check import (
    REPEAT_WINDOW,
    HandlerOperatorReplyCheck,
    normalise_reply,
)
from .handler_refusal_row import record_refusal
from .handler_remote_setup_check import HandlerRemoteSetupCheck
from .handler_silence_guard import HandlerSilenceGuard, is_silence_tick

OPTIN_DIR_ENV = "ONEX_SESSION_OPTIN_DIR"
OPTIN_ENV = "ONEX_SESSION_OPTIN"
OPTIN_TOKEN = re.compile(r"^[A-Za-z0-9._-]+$")
MODE_COMMAND = re.compile(
    r"^\s*/?mode(?:\s+(local|remote|auto|show))?\s*[.!]?\s*$", re.IGNORECASE
)
SILENT_COMMAND = re.compile(
    r"^\s*/?silent(?:\s+(on|off|show))?\s*[.!]?\s*$", re.IGNORECASE
)
TAG = "[access-mode]"
TRANSCRIPT_TAIL_BYTES = 1 << 20


def _transcript_entrypoint(payload: Mapping[str, Any]) -> str:
    path = payload.get("transcript_path")
    if not isinstance(path, str) or not path or not Path(path).is_file():
        return ""  # a regular file only: a pipe or a device could block the hook
    try:
        with open(path, encoding="utf-8") as handle:
            for i, line in enumerate(handle):
                if i >= 40:
                    break
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and isinstance(row.get("entrypoint"), str):
                    return str(row["entrypoint"])
    except OSError:
        return ""
    return ""


def _transcript_events(path: str) -> list[str | None]:
    """The tail of a session transcript as events, oldest first: a reply's text, or None for a
    tool call. Only the main thread counts; an unreadable file or line yields nothing."""
    if not path or not Path(path).is_file():
        return []  # a regular file only: a pipe or a device could block the hook
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            start = max(0, size - TRANSCRIPT_TAIL_BYTES)
            handle.seek(start)
            lines = handle.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return []
    events: list[str | None] = []
    for line in lines[1:] if start else lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        message = row.get("message") if isinstance(row, dict) else None
        if (
            row.get("type") != "assistant"
            or row.get("isSidechain")
            or not isinstance(message, dict)
        ):
            continue
        blocks = message.get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                events.append(None)
            elif block.get("type") == "text" and str(block.get("text") or "").strip():
                events.append(str(block["text"]))
    return events


def recent_replies(payload: Mapping[str, Any], text: str) -> tuple[str, ...]:
    """Up to three replies before ``text`` with no tool call since, oldest first.

    The transcript already holds ``text`` as its last reply when the hook runs; that copy is not
    one of the earlier replies.
    """
    events = _transcript_events(str(payload.get("transcript_path") or ""))
    if (
        events
        and events[-1] is not None
        and normalise_reply(events[-1]) == normalise_reply(text)
    ):
        events.pop()
    replies: list[str] = []
    for event in reversed(events):
        if event is None or len(replies) == REPEAT_WINDOW:
            break
        replies.append(event)
    return tuple(reversed(replies))


def turn_texts(payload: Mapping[str, Any], text: str) -> tuple[str, ...]:
    """Main-turn text blocks since the last user prompt; tool results do not start a turn."""
    path = str(payload.get("transcript_path") or "")
    if not path or not Path(path).is_file():
        return (text,)
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return (text,)
    texts: list[str] = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("isSidechain"):
            continue
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        if row.get("type") == "user":
            if isinstance(content, str) or any(
                isinstance(block, dict) and block.get("type") == "text"
                for block in blocks
            ):
                texts.clear()
        elif row.get("type") == "assistant":
            if isinstance(content, str):
                texts.append(content)
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    value = block.get("text")
                    if isinstance(value, str):
                        texts.append(value)
    return (*texts, text)


def resolve(
    payload: Mapping[str, Any], environ: Mapping[str, str]
) -> ModelAccessModeResult:
    skip = environ.get("CLAUDE_CODE_ENTRYPOINT") or environ.get("OMNI_ACCESS_MODE")
    entry = "" if skip else _transcript_entrypoint(payload)
    return HandlerAccessModeDetect().handle(
        ModelAccessModeRequest(
            session_id=str(payload.get("session_id") or ""),
            state_dir=state_dir(environ),
            environ=dict(environ),
            transcript_entrypoint=entry,
        )
    )


def _emit(event: str, context: str, message: str = "") -> None:
    body: dict[str, Any] = {
        "hookSpecificOutput": {"hookEventName": event, "additionalContext": context}
    }
    if message:
        body["systemMessage"] = message
    print(json.dumps(body))


def _rules_line(mode: EnumAccessMode) -> str:
    kinds = ", ".join(k.rstrip(":") for k in ATTENTION_KINDS)
    paths = (
        "no local file paths (the operator cannot open them)"
        if mode is EnumAccessMode.REMOTE
        else "local paths are fine (clickable at the terminal)"
    )
    push = (
        "an attention item is pushed"
        if mode is EnumAccessMode.REMOTE
        else "an attention item is shown in the session, pushed only after 30 idle minutes"
    )
    return (
        f"Rules in this mode: name every ticket and PR in plain words with the id beside it; "
        f"{paths}; push only for {kinds}, and {push}; never wait in the foreground. "
        "The operator switches with the prompt 'mode local', 'mode remote' or 'mode auto'."
    )


def _read_json(path: Path) -> dict[str, Any]:
    try:
        loaded: object = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def session_opted_in(session_id: str, environ: Mapping[str, str]) -> bool:
    """Whether this session opted in to session start (OMN-21149, operator ruling).

    Session start is optional and manual: the session-start skill writes the session's id to the
    shared marker directory, or the session was launched with ONEX_SESSION_OPTIN=1;
    ONEX_SESSION_OPTIN=0 keeps it out. The default is out.
    """
    decided = environ.get(OPTIN_ENV, "").strip().lower()
    if decided in {"1", "on", "true"}:
        return True
    if decided in {"0", "off", "false"}:
        return False
    if not OPTIN_TOKEN.match(session_id):
        return False
    override = environ.get(OPTIN_DIR_ENV, "").strip()
    home = environ.get("HOME", "").strip()
    if override:
        directory = Path(override)
    elif home:
        directory = Path(home) / ".local" / "state" / "onex" / "session-opt-in"
    else:
        return False
    return (directory / session_id).is_file()


def on_session_start(payload: Mapping[str, Any], environ: Mapping[str, str]) -> int:
    result = resolve(payload, environ)
    if not result.operator_facing:
        return 0
    directory = state_dir(environ)
    session_id = str(payload.get("session_id") or "")
    update_session(
        directory,
        session_id,
        mode=result.mode.value,
        source=result.source,
        detected=result.detected.value if result.detected else None,
        signals=list(result.signals),
    )
    prune_stale_sessions(directory)
    # The mode is recorded for every session, because the Bash, push and reply guards read it; the
    # banner and the remote setup check are orchestrator startup output, opt-in like the rest.
    if not session_opted_in(session_id, environ):
        return 0
    context = f"{TAG} Access mode: {result.describe()}. {_rules_line(result.mode)}"
    message = ""
    if result.mode is EnumAccessMode.REMOTE:
        home = Path.home()
        setup = HandlerRemoteSetupCheck().handle(
            ModelRemoteSetupRequest(
                permission_mode=str(payload.get("permission_mode") or ""),
                settings=_read_json(home / ".claude" / "settings.json"),
                user_config=_read_json(home / ".claude.json"),
                cwd=str(payload.get("cwd") or ""),
                environ=dict(environ),
            )
        )
        context += f" {setup.line}"
        if not setup.ok:
            message = f"{TAG} {setup.line}"
    _emit("SessionStart", context, message)
    return 0


def on_prompt(payload: Mapping[str, Any], environ: Mapping[str, str]) -> int:
    result = resolve(payload, environ)
    if not result.operator_facing:
        return 0
    directory = state_dir(environ)
    session_id = str(payload.get("session_id") or "")
    prompt = str(payload.get("prompt") or "")
    update_session(
        directory,
        session_id,
        last_prompt_at=time.time(),
        silence_tick=is_silence_tick(prompt),
    )
    silent = SILENT_COMMAND.match(prompt)
    if silent:
        word = (silent.group(1) or "show").lower()
        if word != "show":
            update_session(directory, session_id, silent=word == "on")
        enabled = read_global_silent(directory) or read_session(
            directory, session_id
        ).get("silent", False)
        line = f"{TAG} Silent mode is {'on' if enabled else 'off'} for this session."
        _emit(
            "UserPromptSubmit",
            f"{line} The operator's prompt was the silent command itself and the hook"
            " has already confirmed it to them: end the turn with no text.",
            line,
        )
        return 0
    match = MODE_COMMAND.match(prompt)
    if not match:
        return 0
    word = (match.group(1) or "show").lower()
    if word in {"local", "remote"}:
        update_session(directory, session_id, override=word)
    elif word == "auto":
        update_session(directory, session_id, override=None)
    now = resolve(payload, environ)
    line = f"{TAG} Access mode for this session: {now.describe()}."
    _emit(
        "UserPromptSubmit",
        f"{line} {_rules_line(now.mode)} The operator's prompt was the mode command itself:"
        " confirm the mode in one line and do nothing else.",
        line,
    )
    return 0


def _record(
    verdict: ModelHookVerdict, payload: Mapping[str, Any], environ: Mapping[str, str]
) -> None:
    """One refusal row for a refused verdict (OMN-21020); never changes the refusal."""
    try:
        record_refusal(
            verdict.guard, verdict.reason_class, verdict.reason, payload, environ
        )
    except Exception:  # a broken recorder must never turn a refusal into an allow
        return


def _refuse(
    verdict: ModelHookVerdict, payload: Mapping[str, Any], environ: Mapping[str, str]
) -> int:
    _record(verdict, payload, environ)
    print(verdict.reason, file=sys.stderr)
    return 2


def _idle_seconds(state: Mapping[str, Any]) -> float | None:
    last = state.get("last_prompt_at")
    if isinstance(last, (int, float)):
        return max(0.0, time.time() - float(last))
    return None


def on_pre_tool(payload: Mapping[str, Any], environ: Mapping[str, str]) -> int:
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    if tool in {"Workflow", "Agent"}:
        if not mac_route_count(str(tool), tool_input):
            return 0  # the pool is read only for a call that names the Mac
        verdict = HandlerLabDispatchGate().handle(
            ModelLabDispatchRequest(str(tool), tool_input, read_lab_pool(environ))
        )
        return 0 if verdict.allowed else _refuse(verdict, payload, environ)
    agent_id = payload.get("agent_id")
    main_session = True
    if isinstance(agent_id, str) and agent_id.strip():
        main_session = False
    if tool == "Bash" or tool in FETCH_TOOLS:
        command = tool_input.get("command") if tool == "Bash" else ""
        if not isinstance(command, str) or not main_session:
            return 0
        timeout = tool_input.get("timeout")
        result = resolve(payload, environ)
        verdict = HandlerForegroundBashGuard().handle(
            ModelForegroundBashRequest(
                command=command,
                main_session=True,
                operator_facing=result.operator_facing,
                timeout_ms=int(timeout) if isinstance(timeout, (int, float)) else None,
                permission_mode=str(payload.get("permission_mode") or ""),
                tool_name=str(tool),
            )
        )
        return 0 if verdict.allowed else _refuse(verdict, payload, environ)
    if tool == "PushNotification":
        message = tool_input.get("message")
        if not isinstance(message, str):
            return 0
        result = resolve(payload, environ)
        state = read_session(state_dir(environ), str(payload.get("session_id") or ""))
        verdict = HandlerAttentionPushGate().handle(
            ModelPushGateRequest(message, result.mode, _idle_seconds(state))
        )
        return 0 if verdict.allowed else _refuse(verdict, payload, environ)
    if tool == "SendUserMessage" and main_session:
        text = (
            tool_input.get("message")
            or tool_input.get("text")
            or tool_input.get("content")
        )
        if not isinstance(text, str):
            return 0
        result = resolve(payload, environ)
        if not result.operator_facing:
            return 0
        verdict = HandlerOperatorReplyCheck().handle(
            ModelOperatorReplyRequest(text, result.mode)
        )
        return 0 if verdict.allowed else _refuse(verdict, payload, environ)
    return 0


def on_stop(payload: Mapping[str, Any], environ: Mapping[str, str]) -> int:
    text = payload.get("last_assistant_message")
    if not isinstance(text, str) or not text.strip():
        return 0
    result = resolve(payload, environ)
    if not result.operator_facing:
        return 0
    rewrite_asked = payload.get("stop_hook_active") is True
    if not rewrite_asked:
        directory = state_dir(environ)
        state = read_session(directory, str(payload.get("session_id") or ""))
        silent = bool(
            read_global_silent(directory)
            or state.get("silent")
            or state.get("silence_tick")
        )
        silence = HandlerSilenceGuard().handle(
            ModelSilenceRequest(turn_texts(payload, text) if silent else (), silent)
        )
        if not silence.allowed:
            _record(silence, payload, environ)
            print(json.dumps({"decision": "block", "reason": silence.reason}))
            return 0
    verdict = HandlerOperatorReplyCheck().handle(
        ModelOperatorReplyRequest(
            text, result.mode, recent_replies=recent_replies(payload, text)
        )
    )
    if rewrite_asked and verdict.reason_class != "repeated-reply":
        return 0  # one rewrite per turn: only a repeated reply is refused again
    if not verdict.allowed:
        _record(verdict, payload, environ)
        print(json.dumps({"decision": "block", "reason": verdict.reason}))
    return 0


EVENTS = {
    "session-start": on_session_start,
    "prompt": on_prompt,
    "pre-tool": on_pre_tool,
    "stop": on_stop,
}


def mode_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="onex-access-mode",
        description="Show or set the operator access mode (local or remote) the hooks use.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser(
        "show", help="print the override and, with --session, the mode"
    )
    show.add_argument("--session", default="")
    setter = commands.add_parser(
        "set", help="set local or remote, or auto to detect again"
    )
    setter.add_argument("mode", choices=("local", "remote", "auto"))
    setter.add_argument(
        "--session", default="", help="one session id; omit for every session"
    )
    args = parser.parse_args(argv)
    environ = dict(os.environ)
    directory = state_dir(environ)
    if args.command == "set":
        word = None if args.mode == "auto" else args.mode
        if args.session:
            update_session(directory, args.session, override=word)
            print(f"session {args.session}: access mode {args.mode}")
        else:
            write_global_override(directory, word)
            print(f"every operator-facing session: access mode {args.mode}")
        return 0
    print(
        f"global override: {read_global_override(directory) or 'none (detection decides)'}"
    )
    if args.session:
        result = resolve({"session_id": args.session}, environ)
        print(f"session {args.session}: {result.describe()}")
    return 0


def silent_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set silent mode for operator turns.")
    parser.add_argument("silent", choices=("on", "off"))
    parser.add_argument("--session", default="")
    args = parser.parse_args(argv)
    directory = state_dir(os.environ)
    enabled = args.silent == "on"
    if args.session:
        update_session(directory, args.session, silent=enabled)
        print(f"session {args.session}: silent mode {args.silent}")
    else:
        write_global_silent(directory, enabled)
        print(f"every operator-facing session: silent mode {args.silent}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "mode":
        return mode_main(args[1:])
    if args and args[0] == "silent":
        return silent_main(args[1:])
    handler = EVENTS.get(args[0] if args else "")
    if handler is None:
        print(
            f"usage: handler_operator_hook_cli {{{'|'.join(EVENTS)}|mode|silent}}",
            file=sys.stderr,
        )
        return 0
    try:
        loaded: object = json.loads(sys.stdin.read() or "{}")
    except (
        Exception
    ):  # unreadable or pathological input (deep nesting) passes, never wedges
        loaded = {}
    payload = loaded if isinstance(loaded, dict) else {}
    try:
        return handler(payload, os.environ)
    except Exception as exc:  # a broken check must never wedge the session
        print(
            json.dumps(
                {"systemMessage": f"{TAG} hook error ({type(exc).__name__}: {exc})"}
            )
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
