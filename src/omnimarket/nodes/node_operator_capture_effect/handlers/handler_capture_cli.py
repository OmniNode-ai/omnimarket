# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one command every operator-capture entry point calls (OMN-20905, OMN-20906, OMN-20907).

``python -m omnimarket.nodes.node_operator_capture_effect <command>``:

* ``ingest`` (UserPromptSubmit, payload on stdin): take the operator's prompt into the local
  inbox and start a detached worker. Headless sessions (lanes, child sessions, scripted runs)
  and machine injections are skipped. Never blocks the prompt, whatever fails.
* ``guard`` (PreToolUse on Workflow, Agent and Task, payload on stdin): exit 2 with the reason
  when the main session dispatches work from an operator message nobody captured; the message
  is captured in the same act. Its own failures allow the call.
* ``session-start`` (SessionStart): print the open-ask digest into the session's start block,
  push asks waiting over a day, and start a worker for anything still in the inbox.
* ``process``: one worker run over the inbox (what ``ingest`` starts).
* ``digest``: print the digest; ``--push-overdue`` also sends the attention push.
* ``backfill --transcript PATH``: take every operator message of a transcript into the inbox
  (``--since`` bounds it), so a session started before the hook existed is captured too.

Environment: ``ONEX_LEDGER_PATH`` (the ledger), ``ONEX_OPERATOR_CAPTURE_LEDGER_BIN`` (its
append command), ``ONEX_OPERATOR_CAPTURE_ONEX_BIN`` (the onex CLI for delegation, else ``onex``
on PATH), ``ONEX_OPERATOR_CAPTURE_DIR`` (the store), ``ONEX_ALERT_CHANNEL_LIB`` and
``ONEX_ALERT_CHANNEL_ENV_FILE`` (the push channel).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_operator_capture_effect.handlers import capture_store

ALERT_LIB_ENV = "ONEX_ALERT_CHANNEL_LIB"
ALERT_ENV_FILE_ENV = "ONEX_ALERT_CHANNEL_ENV_FILE"
_ALERT_COMMAND = (
    'set -a; [ -n "$1" ] && [ -r "$1" ] && . "$1" >/dev/null 2>&1; set +a; '
    '. "$2" && alert_channel_send "$3" "$4"'
)


def _out(text: str, *, err: bool = False) -> None:
    (sys.stderr if err else sys.stdout).write(text + "\n")


def _read_payload() -> dict[str, Any]:
    try:
        loaded: object = json.loads(sys.stdin.read() or "{}")
    except (OSError, ValueError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _ledger_path() -> Path | None:
    value = os.environ.get("ONEX_LEDGER_PATH", "").strip()
    return Path(value) if value else None


def spawn_worker(root: Path) -> None:
    """Start one detached worker; its output goes to the store's worker log."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / "worker.log").open("a", encoding="utf-8") as log:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnimarket.nodes.node_operator_capture_effect",
                "process",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env=os.environ.copy(),
        )


def alert_channel_push(text: str) -> tuple[bool, str]:
    lib = os.environ.get(ALERT_LIB_ENV, "").strip()
    if not lib:
        return False, f"{ALERT_LIB_ENV} is unset: no push channel"
    env_file = os.environ.get(ALERT_ENV_FILE_ENV, "").strip()
    try:
        done = subprocess.run(
            ["bash", "-c", _ALERT_COMMAND, "_", env_file, lib, "operator-asks", text],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"push did not run: {exc}"
    return done.returncode == 0, f"alert channel exit {done.returncode}"


def cmd_ingest(root: Path) -> int:
    from omnimarket.nodes.node_operator_capture_compute.handlers.handler_utterance_classify import (
        is_machine_injection,
    )

    payload = _read_payload()
    prompt = payload.get("prompt")
    session_id = payload.get("session_id")
    mode = capture_store.session_mode(os.environ)
    if (
        mode == "headless"
        or not isinstance(prompt, str)
        or is_machine_injection(prompt)
    ):
        return 0
    transcript = payload.get("transcript_path")
    capture_store.ingest(
        root,
        session_id=session_id
        if isinstance(session_id, str) and session_id
        else "unknown",
        text=prompt,
        source=f"claude-code:{mode}",
        received_at=datetime.now(UTC),
        origin_event="UserPromptSubmit",
        transcript_path=transcript if isinstance(transcript, str) else None,
    )
    spawn_worker(root)
    return 0


def cmd_guard(root: Path) -> int:
    from omnimarket.models.operator_capture import (
        EnumGuardVerdict,
        ModelCaptureGuardRequest,
    )
    from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_guard import (
        HandlerCaptureGuard,
    )

    payload = _read_payload()
    verdict = HandlerCaptureGuard().handle(
        ModelCaptureGuardRequest(
            store_dir=root,
            payload=payload,
            env={k: os.environ[k] for k in capture_store.ENV_KEYS if k in os.environ},
        )
    )
    if verdict.verdict is EnumGuardVerdict.REFUSE:
        spawn_worker(root)
        _out(verdict.reason, err=True)
        return 2
    return 0


def _digest(root: Path, push: bool) -> tuple[str, Any]:
    from omnimarket.models.operator_capture import ModelCaptureDigestRequest
    from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_digest import (
        HandlerCaptureDigest,
    )

    ledger = _ledger_path()
    if ledger is None:
        return "Open operator asks: unknown (ONEX_LEDGER_PATH is unset).", None
    result = HandlerCaptureDigest(push=alert_channel_push).handle(
        ModelCaptureDigestRequest(
            store_dir=root, ledger_path=ledger, now=datetime.now(UTC), push_overdue=push
        )
    )
    return result.digest, result


def cmd_session_start(root: Path) -> int:
    if capture_store.session_mode(os.environ) == "headless":
        return 0
    if capture_store.pending(root):
        spawn_worker(root)
    digest, _result = _digest(root, push=True)
    _out(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": f"[operator-capture] {digest}",
                }
            }
        )
    )
    return 0


def cmd_process(root: Path, max_captures: int, delegate: bool) -> int:
    from omnimarket.models.operator_capture import ModelCaptureProcessRequest
    from omnimarket.nodes.node_operator_capture_effect.handlers.capture_ports import (
        onex_delegate_runner,
        onex_ledger_appender,
    )
    from omnimarket.nodes.node_operator_capture_effect.handlers.handler_capture_process import (
        HandlerCaptureProcess,
    )

    ledger = _ledger_path()
    if ledger is None:
        _out(
            "operator-capture: ONEX_LEDGER_PATH is unset; the inbox is kept",
            err=True,
        )
        return 3
    result = HandlerCaptureProcess(
        onex_delegate_runner(), onex_ledger_appender()
    ).handle(
        ModelCaptureProcessRequest(
            store_dir=root,
            ledger_path=ledger,
            max_captures=max_captures,
            delegate=delegate,
        )
    )
    _out(
        json.dumps(
            {"at": datetime.now(UTC).isoformat(), **result.model_dump(mode="json")}
        )
    )
    return 0 if not result.errors or result.processed else 1


def cmd_backfill(
    root: Path,
    transcript: Path,
    session: str | None,
    since: str | None,
    source: str | None,
) -> int:
    from omnimarket.nodes.node_operator_capture_effect.handlers.transcript_read import (
        operator_messages,
    )

    floor = datetime.fromisoformat(since.replace("Z", "+00:00")) if since else None
    session_id = session or transcript.stem
    if source is None:
        mode = capture_store.session_mode(os.environ)
        source = f"claude-code:{'remote' if mode == 'headless' else mode}"
    taken = 0
    for text, when in operator_messages(transcript, tail_bytes=None):
        if floor is not None and (when is None or when < floor):
            continue
        if capture_store.ingest(
            root,
            session_id=session_id,
            text=text,
            source=source,
            received_at=when or datetime.now(UTC),
            origin_event="backfill",
            transcript_path=str(transcript),
        ):
            taken += 1
    _out(json.dumps({"backfilled": taken, "session": session_id}))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator-capture", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("ingest")
    sub.add_parser("guard")
    sub.add_parser("session-start")
    process = sub.add_parser("process")
    process.add_argument("--max", type=int, default=20)
    process.add_argument("--no-delegate", action="store_true")
    digest = sub.add_parser("digest")
    digest.add_argument("--push-overdue", action="store_true")
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--transcript", type=Path, required=True)
    backfill.add_argument("--session")
    backfill.add_argument("--since")
    backfill.add_argument("--source", help="claude-code:local or claude-code:remote")
    args = parser.parse_args(argv)
    root = capture_store.store_dir()
    if args.command in {"ingest", "guard", "session-start"}:
        # Hook entry points: their own failure never blocks the operator or the session.
        try:
            if args.command == "ingest":
                return cmd_ingest(root)
            if args.command == "guard":
                return cmd_guard(root)
            return cmd_session_start(root)
        except Exception as exc:  # fail open: a hook must never wedge a session
            _out(f"operator-capture {args.command}: {exc}", err=True)
            return 0
    if args.command == "process":
        return cmd_process(root, args.max, not args.no_delegate)
    if args.command == "digest":
        text, _ = _digest(root, args.push_overdue)
        _out(text)
        return 0
    return cmd_backfill(root, args.transcript, args.session, args.since, args.source)


__all__ = ["main"]
